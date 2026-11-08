"""Offline synthetic preview privacy, layout-contract and path-safety regression tests."""
from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))
from pbz import preview  # noqa: E402
from pbz import consent  # noqa: E402
from pbz.common import PbzError  # noqa: E402


def literal(value):
    return {'expr': {'Literal': {'Value': value}}}


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding='utf-8')


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pbz-preview-fake-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'workspace'
        self.mirror = self.root / 'pbz-workspace'
        self.report = self.mirror / 'Synthetic.Report'
        self.page = self.report / 'definition/pages/p1'
        save(self.page / 'page.json', {'name': 'p1', 'displayName': 'Synthetic page', 'width': 400, 'height': 240})
        self.ws = SimpleNamespace(root=str(self.root), mirror=str(self.mirror))
        self.project = SimpleNamespace(name='Synthetic', mirror_report=str(self.report))

    def visual(self, name, kind, box, title=None, font=10, **extra):
        obj = {'name': name, 'position': dict(zip(['x','y','width','height'], box)),
               'visual': {'visualType': kind, 'objects': {}, 'visualContainerObjects': {}}}
        if title:
            obj['visual']['visualContainerObjects']['title'] = [{'properties': {
                'text': literal("'" + title + "'"), 'fontSize': literal(str(font) + 'D')}}]
        obj.update(extra)
        save(self.page / 'visuals' / name / 'visual.json', obj)
        return obj

    def codes(self, result):
        return {f['code'] for f in result['findings']}

    def test_synthetic_svg_never_copies_labels_values_or_images(self):
        secret = 'CANARY PRIVATE BUSINESS TITLE'
        obj = self.visual('card1', 'card', [110,20,120,80], secret)
        obj['visual']['objects']['unsafe'] = [{'properties': {
            'payload': literal("'data:image/png;base64,CANARYPAYLOAD'"),
            'amount': literal('987654D')}}]
        save(self.page / 'visuals/card1/visual.json', obj)
        self.visual('image1', 'image', [250,20,80,80])
        with patch('pbz.common.engine_call', side_effect=AssertionError('Engine forbidden')):
            result = preview.run(self.ws, self.project)
        record = result['pages'][0]
        svg = Path(record['svg']).read_text(encoding='utf-8')
        self.assertIn('SYNTHETIC PREVIEW', svg)
        self.assertIn('123', svg)
        self.assertNotIn(secret, svg)
        self.assertNotIn('987654', svg)
        self.assertNotIn('CANARYPAYLOAD', svg)
        self.assertNotIn('data:image', svg)
        ET.fromstring(svg)
        self.assertIn('ASSET_PREVIEW_UNSUPPORTED', self.codes(result))
        self.assertEqual(record['native_visual_acceptance'], 'NOT_RUN')
        self.assertEqual(record['images'], 'WITHHELD')

    def test_sidebar_containment_checks_buttons_and_slicers(self):
        self.visual('button', 'actionButton', [4,10,115,30])
        self.visual('filter', 'slicer', [4,50,115,50])
        result = preview.run(self.ws, self.project, spec={'regions': {'sidebar': [0,0,100,240]}})
        spill = [f['visual'] for f in result['findings'] if f['code'] == 'OUTSIDE_REGION']
        self.assertEqual(set(spill), {'button','filter'})
        self.assertEqual(result['gate'], 'FAIL')

    def test_shapes_overlays_require_explicit_exemption(self):
        self.visual('under', 'card', [100,20,140,80])
        self.visual('over', 'actionButton', [100,20,140,80])
        self.assertIn('UNDECLARED_OVERLAP', self.codes(preview.run(self.ws, self.project)))
        result = preview.run(self.ws, self.project, spec={'allow_overlaps': [['under','over']]})
        self.assertNotIn('UNDECLARED_OVERLAP', self.codes(result))

    def test_font_fit_and_regions_use_numeric_geometry_only(self):
        self.visual('long', 'textbox', [100,10,80,22], 'CANARY' * 40, font=22)
        self.visual('sparse', 'card', [110,80,20,30], 'Metric', font=7)
        spec = {'regions': {'body': [100,0,300,240]}, 'min_region_fill': {'body': .8}, 'min_font_pt': 9}
        result = preview.run(self.ws, self.project, spec=spec)
        self.assertIn('TEXT_CLIPPING_RISK', self.codes(result))
        self.assertIn('FONT_BELOW_MINIMUM', self.codes(result))
        self.assertIn('REGION_UNDERUSED', self.codes(result))
        # Font-free source text is never returned by lint either.
        self.assertNotIn('CANARY', json.dumps(result))
        self.assertLess(result['pages'][0]['region_fill']['body'], .8)

    def test_custom_visual_and_background_are_explicitly_unsupported(self):
        self.visual('custom', 'denebFAKE', [100,20,200,100])
        page = {'name':'p1','displayName':'Synthetic page','width':400,'height':240,
                'objects':{'background':[{'properties':{'image':{'image':{'url':literal("'CANARY'\n")}}}}]}}
        save(self.page/'page.json', page)
        result = preview.run(self.ws, self.project)
        self.assertIn('CUSTOM_VISUAL_RENDER_UNSUPPORTED', self.codes(result))
        self.assertIn('BACKGROUND_ASSET_WITHHELD', self.codes(result))
        self.assertEqual(result['pages'][0]['custom_visual_rendering'], 'UNSUPPORTED')

    def test_page_selection_and_deterministic_output(self):
        self.visual('card', 'card', [100,20,200,100])
        result = preview.run(self.ws, self.project, page='Synthetic page')
        first = Path(result['pages'][0]['svg']).read_bytes()
        second = preview.run(self.ws, self.project, page='p1')
        self.assertEqual(first, Path(second['pages'][0]['svg']).read_bytes())
        with self.assertRaises(PbzError):
            preview.run(self.ws, self.project, page='../outside')

    def test_contract_rejects_data_assets_unknown_keys_and_bad_types(self):
        self.visual('card', 'card', [100,20,200,100])
        bad = [{'data':[]}, {'asset':'anything.png'}, {'regions':{'x':[0,0,0,2]}},
               {'regions':{'x':[0,0,float('nan'),2]}}, {'assignments':{'card':'missing'}},
               {'allow_overlaps':[['one']]}, {'min_font_pt':True}, {'version':2}, {'version':True}]
        for spec in bad:
            with self.subTest(spec=spec), self.assertRaises(PbzError):
                preview.run(self.ws, self.project, spec=spec)

    def test_contract_path_must_be_inside_mirror(self):
        outside = Path(self.tmp.name)/'contract.json'
        save(outside,{})
        with self.assertRaises(PbzError) as raised:
            preview.run(self.ws,self.project,spec=str(outside))
        self.assertEqual(raised.exception.code,'UNSAFE_PATH')

    def test_malformed_json_never_echoes_content(self):
        p=self.page/'visuals/broken/visual.json';p.parent.mkdir(parents=True)
        p.write_text('{CANARY SECRET}',encoding='utf-8')
        with self.assertRaises(PbzError) as raised:
            preview.run(self.ws,self.project)
        self.assertNotIn('CANARY',str(raised.exception))

    def test_output_symlinks_are_rejected(self):
        self.visual('card','card',[100,20,200,100])
        outside=Path(self.tmp.name)/'outside';outside.mkdir()
        try:
            (self.mirror/'previews').symlink_to(outside,target_is_directory=True)
        except (OSError,NotImplementedError):
            import subprocess
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.mirror/'previews'), str(outside)], capture_output=True)
            if made.returncode != 0:
                self.skipTest('Creating symlinks is unavailable')
        with self.assertRaises(PbzError) as raised:
            preview.run(self.ws,self.project)
        self.assertEqual(raised.exception.code,'UNSAFE_PATH')
        self.assertEqual(list(outside.iterdir()),[])

    def test_temporary_output_symlink_cannot_truncate_external_file(self):
        self.visual('card','card',[100,20,200,100])
        out=self.mirror/'previews';out.mkdir()
        outside=Path(self.tmp.name)/'important.txt';outside.write_text('KEEP',encoding='utf-8')
        name=hashlib.sha256(b'p1').hexdigest()[:20]+'.svg.pbz-tmp'
        link=out/name
        try:
            link.symlink_to(outside)
        except (OSError,NotImplementedError):
            import subprocess
            holder=Path(self.tmp.name)/'hold';holder.mkdir()
            (holder/'kept.txt').write_text('KEEP',encoding='utf-8')
            made=subprocess.run(["cmd","/c","mklink","/J",str(link),str(holder)],capture_output=True)
            if made.returncode!=0:
                self.skipTest('Creating symlinks is unavailable')
            outside=holder/'kept.txt'
        preview.run(self.ws,self.project)
        self.assertEqual(outside.read_text(encoding='utf-8'),'KEEP')
        self.assertTrue(link.exists())

    def test_outline_extents_checked_without_exempting_control(self):
        obj=self.visual('button','actionButton',[2,5,96,30])
        obj['visual']['objects']['outline']=[{'properties':{'show':literal('true'),'weight':literal('8D')}}]
        save(self.page/'visuals/button/visual.json',obj)
        result=preview.run(self.ws,self.project,spec={'regions':{'sidebar':[0,0,100,240]}})
        self.assertIn('OUTLINE_NEAR_REGION_EDGE',self.codes(result))

    def test_missing_assignment_and_outside_region_are_not_silent(self):
        self.visual('card','card',[100,20,200,100])
        result=preview.run(self.ws,self.project,spec={'regions':{'body':[0,0,500,240]},'assignments':{'missing':'body'}})
        self.assertIn('ASSIGNED_VISUAL_NOT_FOUND',self.codes(result))
        self.assertIn('CONTRACT_REGION_OFF_CANVAS',self.codes(result))

    def test_only_unchanged_human_approved_png_can_be_preview_background(self):
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from test_consent import png
        self.visual('card','card',[100,20,200,100])
        self.ws.private=str(Path(self.tmp.name)/'private')
        asset=self.root/'brand.png';asset.write_bytes(png())
        with patch('pbz.consent.dpapi.load_key',return_value=b'fake-preview-key'), \
                patch('pbz.consent.dpapi.protect',side_effect=lambda b:b), \
                patch('pbz.consent.dpapi.unprotect',side_effect=lambda b:b):
            receipt=consent.request(self.ws,str(asset),'brand-asset')
            spec={'asset_receipt':receipt['id'],'regions':{'sidebar':[0,0,100,240]}}
            with self.assertRaises(PbzError):preview.run(self.ws,self.project,spec=spec)
            with patch('pbz.consent._human_terminal',return_value=True):
                consent.approve(self.ws,receipt['id'],human=True,attested=True)
            result=preview.run(self.ws,self.project,spec=spec)
            self.assertEqual(result['pages'][0]['images'],'APPROVED_BACKGROUND_ONLY')
            self.assertEqual(result['pages'][0]['background_sha256'],receipt['sha256'])
            svg=ET.fromstring(Path(result['pages'][0]['svg']).read_text(encoding='utf-8'))
            image=svg.find('{http://www.w3.org/2000/svg}image')
            embedded=base64.b64decode(image.attrib['href'].split(',',1)[1])
            self.assertNotIn(b'CANARY_PNG_METADATA',embedded)
            asset.write_bytes(png(False))
            with self.assertRaises(PbzError):preview.run(self.ws,self.project,spec=spec)

    def test_capability_labels_do_not_claim_native_fit_or_interaction(self):
        self.visual('card', 'card', [20, 20, 100, 80])
        self.visual('custom', 'CustomViz', [140, 20, 100, 80])
        result = preview.run(self.ws, self.project)
        page = result['pages'][0]
        caps = page['capabilities']
        self.assertEqual(caps['layout'], 'geometry_only')
        self.assertEqual(caps['text_fit'], 'unsupported')
        self.assertEqual(caps['interaction'], 'unsupported')
        self.assertEqual(caps['custom_visuals'], 'unsupported')
        by_id = {item['id']: item['rendering'] for item in page['visual_capabilities']}
        self.assertEqual(by_id['card'], 'geometry_only')
        self.assertEqual(by_id['custom'], 'unsupported')
        stored = json.loads(Path(page['metadata']).read_text(encoding='utf-8'))
        self.assertEqual(stored['capabilities'], caps)
        blob = json.dumps(page)
        self.assertNotIn('native_text_fit', blob)
        self.assertNotIn('interaction_certified', blob)


if __name__ == '__main__':
    unittest.main()
