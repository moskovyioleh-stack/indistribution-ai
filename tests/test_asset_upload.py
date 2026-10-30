import base64
import contextlib
import io
import json
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from asset_upload import normalize, AssetError, cdn_url, upload_assets
import test_tilda_call

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'

class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_base64_and_data_uri(self):
        for source in [base64.b64encode(SVG).decode(), 'data:image/svg+xml;base64,' + base64.b64encode(SVG).decode()]:
            result = await normalize({'image_base64': source, 'filename': 'test.svg'})
            self.assertEqual(result['mimetype'], 'image/svg+xml')

    async def test_invalid_content(self):
        for item in [
            {'image_base64': '%%%SENSITIVE%%%'},
            {'image_base64': base64.b64encode(SVG).decode(), 'mimetype': 'image/jpeg'},
            {'image_base64': base64.b64encode(b'%PDF-1.7').decode()},
            {'image_base64': base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>').decode()},
            {'image_base64': base64.b64encode(b'\xff\xd8\xffgarbage').decode()},
            {'image_url': 'https://127.0.0.1/secret'},
            {'image_url': 'https://static.tildacdn.com.evil.test/a'},
            {'image_url': 'https://static.tildacdn.com/a?secret=yes'},
            {'image_base64': 'x', 'image_url': 'https://static.tildacdn.com/a'},
            {'path': '/workspace/file.jpg'},
        ]:
            with self.subTest(item=list(item)):
                with self.assertRaises(AssetError): await normalize(item)

    async def test_limit(self):
        with patch.dict(os.environ, {'TILDA_UPLOAD_MAX_BYTES': '2'}):
            with self.assertRaisesRegex(AssetError, 'size_exceeded'):
                await normalize({'image_base64': base64.b64encode(SVG).decode()})

    def test_url_restrictions(self):
        for u in ['http://static.tildacdn.com/a', 'https://user@static.tildacdn.com/a',
                  'https://static.tildacdn.com:444/a', 'https://[::1]/a']:
            self.assertFalse(cdn_url(u))
        self.assertTrue(cdn_url('https://static.tildacdn.com/test/a.svg'))


class UploadGatewayTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_tilda_call.GatewayTests.setUp
    call = test_tilda_call.GatewayTests.call
    async def test_asset_routes(self):
        original = self.call
        async def call(name, args):
            if name == 'upload_image':
                self.calls.append((name,args))
                return NS(content=[NS(type='text', text=json.dumps({'url':'https://static.tildacdn.com/test/a.svg'}))], is_error=False)
            return await original(name,args)
        self.call = call
        arguments = {'projectid': self.pid, 'images': [{'image_base64': base64.b64encode(SVG).decode()}]}
        for action,payload in [
            ('tilda_call', {'tool':'upload_image', 'arguments':arguments, 'confirmation':'SAVE_DRAFT_ONLY'}),
            ('tilda_upload_assets', {'project_id':self.pid, 'images':arguments['images'], 'confirmation':'SAVE_DRAFT_ONLY'})]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = await self.action(action,payload)
            self.assertEqual(result['status'],'uploaded')
            self.assertEqual(output.getvalue(),'')
        self.assertTrue(all(n in {'get_projects','upload_image'} for n,a in self.calls))
        self.foreign_project=True
        self.assertEqual((await self.action('tilda_call',payload if action=='tilda_call' else
            {'tool':'upload_image','arguments':arguments,'confirmation':'SAVE_DRAFT_ONLY'}))['status'],'forbidden')

    async def test_upload_error_and_missing_url(self):
        args={'projectid':self.pid,'images':[{'image_base64':base64.b64encode(SVG).decode()}]}
        # Existing upstream fake returns a response without a CDN URL.
        self.assertEqual((await upload_assets(args,'SAVE_DRAFT_ONLY',{self.pid}))['status'],'upload_unconfirmed')
        original=self.call
        async def fail(name,args):
            if name=='upload_image': raise TimeoutError('secret file bytes')
            return await original(name,args)
        self.call=fail
        result=await upload_assets(args,'SAVE_DRAFT_ONLY',{self.pid})
        self.assertEqual(result['status'],'upload_unconfirmed')
        self.assertNotIn('secret',str(result))
        self.assertFalse(result['retry_automatically'])
        self.assertEqual((await upload_assets(args,None,{self.pid}))['status'],'confirmation_required')
        self.assertEqual((await upload_assets(args,'SAVE_DRAFT_ONLY',set()))['status'],'forbidden')
