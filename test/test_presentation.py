import ast
import asyncio
import importlib
import logging
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules['astrbot'] = types.ModuleType('astrbot')
api = types.ModuleType('astrbot.api')
api.logger = logging.getLogger('presentation-test')
sys.modules['astrbot.api'] = api
for name, directory in (
    ('core.parser', 'core/parser'),
    ('core.parser.platform', 'core/parser/platform'),
    ('core.parser.platform.wechat', 'core/parser/platform/wechat'),
):
    module = types.ModuleType(name)
    module.__path__ = [str(ROOT / directory)]
    sys.modules[name] = module

class Component:
    def __init__(self, text='', **kwargs):
        self.text = text
        self.__dict__.update(kwargs)

    @classmethod
    def fromURL(cls, value):
        return cls(file=value)

    @classmethod
    def fromFileSystem(cls, value):
        return cls(file=value)

components = types.ModuleType('astrbot.api.message_components')
for name in ('File', 'Plain', 'Image', 'Video', 'Record', 'Node', 'Reply'):
    setattr(components, name, type(name, (Component,), {}))
components.Nodes = type('Nodes', (Component,), {'__init__': lambda self, nodes: setattr(self, 'nodes', nodes)})
sys.modules['astrbot.api.message_components'] = components
event_module = types.ModuleType('astrbot.api.event')
event_module.AstrMessageEvent = object
sys.modules['astrbot.api.event'] = event_module

article = importlib.import_module('core.parser.platform.wechat.article')
builder = importlib.import_module('core.message_adapter.node_builder')
renderer = importlib.import_module('core.message_adapter.text_renderer')
sender = importlib.import_module('core.message_adapter.sender')
Plain, Image = components.Plain, components.Image

class PresentationTests(unittest.TestCase):
    def metadata(self):
        data = article.parse_article_page('''<h1 id="activity-name">排版示例</h1>
        <div id="js_content"><p>第一段<strong>加粗内容</strong></p>
        <img data-src="https://example.com/a.png"><p>第二段</p>
        <img src="https://example.com/b.png"><p>第三段</p>
        <img src="https://example.com/a.png"><p>结束</p><script>隐藏文本</script></div>''',
        'https://mp.weixin.qq.com/s/test')
        data.update(has_valid_media=True, image_modes=['direct', 'direct'])
        return data

    def test_article_order_repeated_images_and_text(self):
        data = self.metadata()
        self.assertEqual([b['type'] for b in data['article_blocks']],
                         ['text', 'image', 'text', 'image', 'text', 'image', 'text'])
        self.assertNotIn('隐藏', data['desc'])
        result = builder.build_all_nodes([data])
        nodes = result.all_link_nodes[0]
        self.assertEqual([n.file for n in nodes if isinstance(n, Image)],
                         ['https://example.com/a.png', 'https://example.com/b.png', 'https://example.com/a.png'])
        self.assertNotIn('第一段', nodes[0].text)
        self.assertTrue(result.link_metadata[0]['preserve_order'])

    def test_skipped_image_does_not_shift_following_image(self):
        data = self.metadata()
        data['image_modes'] = ['skip', 'direct']
        nodes = builder.build_all_nodes([data]).all_link_nodes[0]
        self.assertEqual([n.file for n in nodes if isinstance(n, Image)], ['https://example.com/b.png'])
        image_index = next(i for i, n in enumerate(nodes) if isinstance(n, Image))
        self.assertIn('第二段', nodes[image_index - 1].text)

    def test_output_switches(self):
        data = self.metadata()
        data['_enable_text_metadata'] = False
        nodes = builder.build_all_nodes([data]).all_link_nodes[0]
        self.assertTrue(all(isinstance(n, Image) for n in nodes))
        data['_enable_text_metadata'] = True
        data['_enable_rich_media'] = False
        nodes = builder.build_all_nodes([data]).all_link_nodes[0]
        self.assertTrue(all(isinstance(n, Plain) for n in nodes))
        self.assertEqual(sum('第一段' in n.text for n in nodes), 1)

    def test_comments(self):
        node = builder.build_hot_comments_node({'hot_comments': [None, {'username': '小明', 'uid': '123', 'message': '很好看', 'likes': 0}]})
        self.assertIn('1 条', node.text)
        self.assertNotIn('uid:', node.text)
        self.assertIn('赞 0', node.text)

    def test_bounded_pages_and_complete_text(self):
        from PIL import Image as PILImage, ImageDraw
        original = ImageDraw.ImageDraw.text
        drawn = []
        def record(draw, xy, text, *args, **kwargs):
            drawn.append(text)
            return original(draw, xy, text, *args, **kwargs)
        with tempfile.TemporaryDirectory(dir=ROOT / 'test') as directory:
            with patch.object(ImageDraw.ImageDraw, 'text', record):
                paths = renderer._render_text_metadata_image_sync(
                    '\n\n'.join(f'段落{i:03d}：' + '中文正文测试' * 12 for i in range(90)),
                    Path(directory) / 'article.png', '公众号文章', 960, 24, 'card', 'noto_sans', True)
            self.assertGreater(len(paths), 2)
            for path in paths:
                with PILImage.open(path) as image:
                    self.assertLessEqual(image.height, 1800)
                    self.assertEqual(image.width, 960)
            for i in range(90):
                self.assertEqual(sum(f'段落{i:03d}：' in line for line in drawn), 1)

class AsyncPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sending_preserves_order(self):
        nodes = [Plain('开头'), Image(file='a'), Plain('中间'), Image(file='b')]
        meta = [{'link_nodes': nodes, 'preserve_order': True, 'is_normal': True}]
        class Event:
            def __init__(self): self.sent = []
            def chain_result(self, nodes): return nodes
            def plain_result(self, text): return text
            async def send(self, message): self.sent.append(message)
        event = Event()
        await sender.MessageSender().send_individual_results(event, [nodes], meta)
        self.assertEqual([message[0] for message in event.sent], nodes)
        event = Event()
        await sender.MessageSender().send_aggregated_results(event, meta, 'bot', 1)
        self.assertEqual([node.content[0] for node in event.sent[0][0].nodes], nodes)

    async def test_render_in_place_and_fallback(self):
        tree = ast.parse((ROOT / 'main.py').read_text(encoding='utf-8'))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_render_text_metadata_output')
        scope = dict(Path=Path, tempfile=tempfile, uuid=uuid, asyncio=asyncio,
                     Plain=Plain, Image=Image, render_text_metadata_images=AsyncMock(side_effect=[['page1', 'page2'], RuntimeError('failure')]))
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'main.py', 'exec'), scope)
        first, second, photo = Plain('正文'), Plain('保留文本'), Image(file='photo')
        nodes = [first, photo, second]
        result = types.SimpleNamespace(all_link_nodes=[nodes], temp_files=[],
                    link_metadata=[dict(link_nodes=nodes, metadata_text_node=first)])
        cfg = types.SimpleNamespace(message=types.SimpleNamespace(text_metadata=types.SimpleNamespace(
              render_to_image=True, paginate_images=True, separate_text_sections=True, render_style='card', render_font_family='noto_sans', render_font_size=24)),
              download=types.SimpleNamespace(cache_dir=str(ROOT / 'test')), relay=types.SimpleNamespace(enabled=False))
        await scope['_render_text_metadata_output'](types.SimpleNamespace(logger=logging.getLogger()), result, [], cfg)
        self.assertEqual([n.file for n in nodes[:3]], ['page1', 'page2', 'photo'])
        self.assertIs(nodes[3], second)
        self.assertEqual(result.temp_files, ['page1', 'page2'])
        self.assertIs(result.link_metadata[0]['metadata_text_node'], nodes[0])

if __name__ == '__main__':
    unittest.main()
