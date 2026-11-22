"""DPAPI lazy binding, test harness seam, and native interoperability regressions."""
import os
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, 'tests'))

import subprocess
import tempfile
import textwrap
import unittest


class DpapiSeamTests(unittest.TestCase):
    def _child(self, code, env_overlay, *args):
        env = os.environ.copy()
        env.pop('PBZ_FAKE_DPAPI', None)
        env.pop('PBZ_TEST_HARNESS', None)
        env.update(env_overlay)
        env['PYTHONPATH'] = SKILL
        return subprocess.run(
            [sys.executable, '-c', textwrap.dedent(code), *args],
            cwd=SKILL, env=env, capture_output=True, text=True, timeout=120)

    def test_import_does_not_bind_windll(self):
        child = self._child('''
            import ctypes
            class _Boom:
                def __init__(self):
                    self.accesses = []
                def __getattr__(self, name):
                    self.accesses.append(name)
                    raise AttributeError("windll sabotaged")
            boom = _Boom()
            ctypes.windll = boom
            import pbz.dpapi
            assert not boom.accesses, boom.accesses
            print("import ok")
        ''', {})
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn('import ok', child.stdout)

    def test_fake_var_without_harness_marker_is_refused(self):
        child = self._child('''
            from pbz import dpapi
            dpapi.protect(b"x")
            print("protected")
        ''', {'PBZ_FAKE_DPAPI': '1'})
        self.assertNotEqual(child.returncode, 0, child.stdout)
        self.assertIn('outside the test harness', child.stderr)
        child = self._child('''
            from pbz import dpapi
            dpapi.unprotect(b"PBZFAKE1\\x00x")
            print("unprotected")
        ''', {'PBZ_FAKE_DPAPI': '1'})
        self.assertNotEqual(child.returncode, 0, child.stdout)
        self.assertIn('outside the test harness', child.stderr)

    @unittest.skipUnless(os.name == 'nt', 'native DPAPI comparison requires Windows')
    def test_fake_output_is_not_native_dpapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'fake.bin')
            fake = self._child('''
                import sys
                from pbz import dpapi
                data = dpapi.protect(b"seam-probe")
                assert dpapi.unprotect(data) == b"seam-probe"
                with open(sys.argv[1], "wb") as handle:
                    handle.write(data)
                print("fake ok")
            ''', {'PBZ_FAKE_DPAPI': '1', 'PBZ_TEST_HARNESS': '1'}, path)
            self.assertEqual(fake.returncode, 0, fake.stderr)
            self.assertIn('fake ok', fake.stdout)
            native = self._child('''
                import ctypes
                import ctypes.wintypes as wt
                import sys
                class DATA_BLOB(ctypes.Structure):
                    _fields_ = [("cbData", wt.DWORD),
                                ("pbData", ctypes.POINTER(ctypes.c_char))]
                fn = ctypes.windll.crypt32.CryptUnprotectData
                fn.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                               wt.DWORD, ctypes.POINTER(DATA_BLOB)]
                fn.restype = wt.BOOL
                free = ctypes.windll.kernel32.LocalFree
                free.argtypes = [ctypes.c_void_p]
                free.restype = ctypes.c_void_p
                with open(sys.argv[1], "rb") as handle:
                    data = handle.read()
                buf = ctypes.create_string_buffer(data, len(data))
                src = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
                out = DATA_BLOB()
                if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out)):
                    sys.exit(3)
                free(ctypes.cast(out.pbData, ctypes.c_void_p))
                print("native read")
            ''', {}, path)
            self.assertEqual(native.returncode, 3, native.stdout + native.stderr)

    @unittest.skipUnless(os.name == 'nt', 'real DPAPI requires Windows')
    def test_real_dpapi_interop_fresh_process(self):
        child = self._child('''
            import ctypes
            import ctypes.wintypes as wt
            from pbz import dpapi
            class DATA_BLOB(ctypes.Structure):
                _fields_ = [("cbData", wt.DWORD),
                            ("pbData", ctypes.POINTER(ctypes.c_char))]
            crypt32 = ctypes.WinDLL("crypt32")
            kernel32 = ctypes.WinDLL("kernel32")
            protect = crypt32.CryptProtectData
            unprotect = crypt32.CryptUnprotectData
            for fn in (protect, unprotect):
                fn.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                               wt.DWORD, ctypes.POINTER(DATA_BLOB)]
                fn.restype = wt.BOOL
            free = kernel32.LocalFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = ctypes.c_void_p
            def raw(fn, data):
                buf = ctypes.create_string_buffer(data, len(data))
                src = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
                out = DATA_BLOB()
                assert fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out))
                try:
                    return ctypes.string_at(out.pbData, out.cbData)
                finally:
                    free(ctypes.cast(out.pbData, ctypes.c_void_p))
            assert raw(unprotect, dpapi.protect(b"seam-probe")) == b"seam-probe"
            assert dpapi.unprotect(raw(protect, b"seam-probe")) == b"seam-probe"
            print("interop ok")
        ''', {})
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn('interop ok', child.stdout)

    def test_fake_provider_covers_key_and_file_call_sites(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = self._child('''
                import ctypes
                class _Boom:
                    def __getattr__(self, name):
                        raise AttributeError("windll sabotaged")
                ctypes.windll = _Boom()
                import os, sys
                from pbz import dpapi
                private = os.path.join(sys.argv[1], "private")
                key1 = dpapi.load_key(private)
                key2 = dpapi.load_key(private)
                assert key1 == key2 and len(key1) == 32
                path = os.path.join(sys.argv[1], "blob.bin")
                dpapi.write_protected(path, b"payload")
                assert dpapi.read_protected(path) == b"payload"
                print("callsites ok")
            ''', {'PBZ_FAKE_DPAPI': '1', 'PBZ_TEST_HARNESS': '1'}, tmp)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn('callsites ok', child.stdout)

    def test_import_pbz_cli_succeeds(self):
        child = self._child('''
            import pbz.cli
            print("cli ok")
        ''', {})
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn('cli ok', child.stdout)


if __name__ == '__main__':
    unittest.main()
