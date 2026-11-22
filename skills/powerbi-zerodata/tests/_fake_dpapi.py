"""Test-only substitute loaded by pbz.dpapi with both PBZ_FAKE_DPAPI=1 and
PBZ_TEST_HARNESS=1, never imported by production paths.

The transform adds a magic prefix; it is not encryption.
"""

MAGIC = b"PBZFAKE1\x00"


def protect(data: bytes) -> bytes:
    return MAGIC + data


def unprotect(data: bytes) -> bytes:
    if not data.startswith(MAGIC):
        raise ValueError("data was not protected by the test fake DPAPI provider")
    return data[len(MAGIC):]
