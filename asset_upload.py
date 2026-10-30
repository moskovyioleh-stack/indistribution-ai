"""Bounded asset upload only. Never logs sources, bytes, tokens or upstream errors."""
import asyncio
import base64
import binascii
import os
import io
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from tilda_contracts import result_data, ContractError


class AssetError(ValueError):
    pass


class _SVGTreeBuilder(ET.TreeBuilder):
    def pi(self, target, text=None):
        # Also catches PIs before/after the root, which root.iter() cannot see.
        raise AssetError('unsafe_svg')


def limit():
    try:
        size = int(os.getenv('TILDA_UPLOAD_MAX_BYTES', '7500000'))
    except ValueError:
        raise AssetError('invalid_size_configuration') from None
    if not 1 <= size <= 20000000:
        raise AssetError('invalid_size_configuration')
    return size


def cdn_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        u = urlsplit(value)
        return (u.scheme == 'https' and u.hostname == 'static.tildacdn.com'
                and u.port in (None, 443) and not u.username and not u.password
                and not u.query and not u.fragment and bool(u.path.strip('/'))
                and not any(ord(c) < 33 or c == '\\' for c in value))
    except ValueError:
        return False


def sniff(data):
    if data.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if data.startswith((b'GIF87a', b'GIF89a')): return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return 'image/webp'
    if data[:4] == b'wOFF': return 'font/woff'
    if data[:4] == b'wOF2': return 'font/woff2'
    if data[4:8] == b'ftyp' and data[8:12] in (b'avif', b'avis'): return 'image/avif'
    # Inspect the same encoding we parse. Byte scans alone miss UTF-16/32 DTDs.
    # Keep this subset UTF-8 only and retain processing instructions so an
    # xml-stylesheet PI cannot disappear during validation of the original bytes.
    try:
        xml = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise AssetError('unsafe_svg') from None
    if '\x00' in xml or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise AssetError('unsafe_svg')
    try:
        parser = ET.XMLParser(target=_SVGTreeBuilder())
        root = ET.fromstring(xml, parser=parser)
    except (ET.ParseError, ValueError):
        raise AssetError('unsupported_format') from None
    if root.tag != '{http://www.w3.org/2000/svg}svg':
        raise AssetError('unsupported_format')
    # Conservative SVG subset: no scripting, external references, CSS or animation.
    allowed = {'svg', 'g', 'defs', 'path', 'rect', 'circle', 'ellipse', 'line',
               'polyline', 'polygon', 'text', 'tspan', 'title', 'desc',
               'linearGradient', 'radialGradient', 'stop', 'clipPath'}
    for node in root.iter():
        if (not isinstance(node.tag, str)
                or not node.tag.startswith('{http://www.w3.org/2000/svg}')):
            raise AssetError('unsafe_svg')
        tag = node.tag.rsplit('}', 1)[-1]
        if tag not in allowed:
            raise AssetError('unsafe_svg')
        for key, value in node.attrib.items():
            key = key.rsplit('}', 1)[-1].lower()
            if (key.startswith('on') or key in {'href', 'src', 'style'}
                    or re.search(r'url\s*\(|javascript:|https?:|data:|@import', value, re.I)):
                raise AssetError('unsafe_svg')
    return 'image/svg+xml'


async def normalize(item):
    if not isinstance(item, dict) or set(item) - {'image_base64', 'image_url', 'mimetype', 'filename'}:
        raise AssetError('invalid_image_fields')
    if ('image_base64' in item) == ('image_url' in item):
        raise AssetError('exactly_one_source_required')
    maximum = limit()
    declared = item.get('mimetype')
    if 'image_url' in item:
        # Fixed trusted CDN only: no arbitrary hosts, userinfo, signed URLs or redirects.
        # This intentionally avoids a DNS-check/fetch race and SSRF through user hosts.
        if not cdn_url(item['image_url']):
            raise AssetError('url_source_not_allowed')
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                async with client.stream('GET', item['image_url']) as response:
                    if response.status_code != 200:
                        raise AssetError('source_unavailable')
                    chunks, count = [], 0
                    async for chunk in response.aiter_bytes():
                        count += len(chunk)
                        if count > maximum: raise AssetError('size_exceeded')
                        chunks.append(chunk)
                    data = b''.join(chunks)
        except httpx.TimeoutException:
            raise AssetError('source_timeout') from None
        except httpx.HTTPError:
            raise AssetError('source_unavailable') from None
    else:
        encoded = item['image_base64']
        if not isinstance(encoded, str): raise AssetError('invalid_base64')
        if len(encoded) > ((maximum + 2) // 3) * 4 + 128:
            raise AssetError('size_exceeded')
        if encoded.startswith('data:'):
            match = re.fullmatch(r'data:([^;,]+);base64,(.*)', encoded, re.S)
            if not match: raise AssetError('invalid_data_uri')
            if declared and declared != match[1]: raise AssetError('mime_mismatch')
            declared, encoded = match.groups()
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise AssetError('invalid_base64') from None
    if not data: raise AssetError('empty_file')
    if len(data) > maximum: raise AssetError('size_exceeded')
    mime = sniff(data)
    if declared and declared != mime and not (mime == 'image/png' and declared == 'image/apng'):
        raise AssetError('mime_mismatch')
    if mime.startswith('image/') and mime != 'image/svg+xml':
        from PIL import Image
        import warnings
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
        except Exception:
            raise AssetError('invalid_image_content') from None
    elif mime.startswith('font/'):
        from fontTools.ttLib import TTFont
        try:
            with TTFont(io.BytesIO(data), lazy=False) as font:
                for tag in font.keys():
                    if tag != 'GlyphOrder': font[tag]
        except Exception:
            raise AssetError('invalid_font_content') from None
    filename = item.get('filename', 'asset')
    if not isinstance(filename, str) or not re.fullmatch(r'[\w .-]{1,120}', filename):
        raise AssetError('invalid_filename')
    return {'image_base64': base64.b64encode(data).decode('ascii'),
            'mimetype': mime, 'filename': filename}


def urls_from(data):
    if isinstance(data, str): return [data] if cdn_url(data) else []
    if isinstance(data, list): return [u for v in data for u in urls_from(v)]
    if isinstance(data, dict): return [u for v in data.values() for u in urls_from(v)]
    return []


async def upload_assets(arguments, confirmation, approved):
    if confirmation != 'SAVE_DRAFT_ONLY': return {'status': 'confirmation_required'}
    if (not isinstance(arguments, dict) or set(arguments) != {'projectid', 'images'}
            or type(arguments.get('projectid')) is not int):
        return {'status': 'invalid_request', 'reason': 'projectid_integer_and_images_required'}
    pid, images = arguments['projectid'], arguments['images']
    if pid not in approved: return {'status': 'forbidden', 'reason': 'Project not approved'}
    if not isinstance(images, list) or not 1 <= len(images) <= 8:
        return {'status': 'invalid_request', 'reason': 'batch_must_contain_1_to_8_assets'}
    token = os.getenv('TILDA_MCP_TOKEN', '').strip()
    if not token: return {'status': 'not_configured'}
    stage = 'validation'
    try:
        normalized = [await normalize(item) for item in images]
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport
        stage = 'connect'
        async with asyncio.timeout(120):
            async with Client(StreamableHttpTransport(
                url='https://tilda.ru/api/mcp/',
                headers={'Authorization': 'Bearer ' + token},
            )) as upstream:
                stage = 'get_projects'
                projects = result_data(await upstream.call_tool('get_projects', {}))
                if not isinstance(projects, list) or not any(
                    isinstance(p, dict) and str(p.get('id')) == str(pid) for p in projects
                ): return {'status': 'forbidden', 'reason': 'Project unavailable to connected Tilda account'}
                stage = 'upload'
                data = result_data(await upstream.call_tool('upload_image', {
                    'projectid': pid, 'images': normalized}))
                urls = list(dict.fromkeys(urls_from(data)))
                if len(urls) != len(images):
                    return {'status': 'upload_unconfirmed', 'reason': 'cdn_url_count_mismatch',
                            'retry_automatically': False}
                return {'status': 'uploaded', 'urls': urls, 'projectid': pid,
                        'published_by_agent': False}
    except AssetError as exc:
        return {'status': 'invalid_request', 'reason': str(exc)}
    except TimeoutError:
        return {'status': 'upload_unconfirmed' if stage == 'upload' else 'upstream_timeout',
                'stage': stage, 'retry_automatically': False}
    except ContractError:
        return {'status': 'upstream_error', 'stage': stage, 'reason': 'tilda_rejected_or_invalid_response'}
    except Exception:
        return {'status': 'upload_unconfirmed' if stage == 'upload' else 'upstream_error',
                'stage': stage, 'retry_automatically': False}
