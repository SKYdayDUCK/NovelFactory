"""Build an explicitly synthetic Chinese long-context fixture, with no LLM calls.

The source units are original hand-authored benchmark data. Neutral paragraphs
expand narrative distance deterministically, not literary realism. Gold labels
live in a separate file and are never consulted while building any index.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def build():
    spec = json.loads((ROOT / 'source_units.json').read_text())
    source_by_chapter = {row['chapter']: row for row in spec['sources']}
    places = ['北堤', '旧钟楼', '盐市', '西泊口', '石阶', '小渡口', '空仓', '茶铺']
    weathers = ['薄雾沿着地面缓缓散开', '风把篷布吹得轻轻作响', '雨水顺着旧瓦滴落', '午后的光停在窗格里']
    sounds = ['车轮碾过石缝', '远处有人收起竹席', '木门慢慢合拢', '鸟雀越过灰色屋檐']
    neutral = []
    chapters = []
    for chapter in range(1, 49):
        paragraphs = []
        anchor = source_by_chapter.get(chapter)
        anchor_slot = (chapter * 5 + 3) % 8
        for slot in range(8):
            if slot == anchor_slot and anchor is not None:
                row = dict(anchor)
                text = row['text']
            else:
                place = places[(chapter + slot) % len(places)]
                weather = weathers[(chapter + 2 * slot) % len(weathers)]
                sound = sounds[(2 * chapter + slot) % len(sounds)]
                text = (
                    f'第{chapter}日的{place}没有赶集。{weather}，{sound}，走过的人都把脚步放慢了些。'
                    f'靠窗的长凳留着一条浅浅的水痕，晾绳上夹着第{slot + 1}块蓝布；布角旋了一圈，又落回原处。'
                    '这段街景没有记录新的交易、秘密或人物态度，只留下可以略去的环境间隔。'
                    '远处的屋脊被雾遮住一半，另一半还映着天色。有人把空篮挪到不挡路的墙边，'
                    '又用指节轻敲了敲木沿。小巷尽头传来几声模糊的招呼，话音散开，听不清指的是谁。'
                    '这里没有额外的世界规则，纸页上的空白也不预告将来一定发生什么。'
                )
                row = {'source_id': f'N{chapter:02d}{slot}', 'chapter': chapter,
                       'tick': chapter, 'valid_from': chapter, 'valid_to': None,
                       'known_at': chapter, 'plane': 'LITERARY', 'actor_id': None,
                       'entities': [], 'category': 'BACKGROUND', 'related': [],
                       'work_id': 'fog_port', 'branch_id': 'canon', 'revisions': ['r1', 'r2'],
                       'reader_from': chapter, 'text': text}
                neutral.append(row['source_id'])
            row['paragraph'] = slot
            row['sha256'] = digest(text)
            row['span'] = {'start': 0, 'end': len(text), 'unit': 'unicode_code_point'}
            paragraphs.append(row)
        chapters.append({'chapter': chapter, 'title': f'雾港纪事·第{chapter}章', 'sources': paragraphs})
    result = {
        'schema_version': 'nf-memory-corpus-1', 'status': 'TEST_FIXTURE',
        'title': '雾港纪事：中文长上下文检索合成语料',
        'method': 'Original authored source units plus deterministic neutral distance expansion; not model output.',
        'generator_version': 'nf-memory-corpus-builder-1',
        'source_manifest_sha256': digest((ROOT / 'source_units.json').read_text()),
        'character_ids': ['lu', 'shen', 'qi', 'meng', 'yan'],
        'aliases': {'lu': ['陆舟'], 'shen': ['沈禾'], 'qi': ['祁雁'], 'meng': ['孟迟'],
                    'yan': ['严渚'], 'key': ['铜钥'], 'lamp': ['朱灯'], 'gate': ['潮门'],
                    'letter': ['雨封信'], 'reed': ['芦绳'], 'seal': ['封签'], 'salt': ['盐袋']},
        'chapters': chapters,
    }
    result['source_count'] = sum(len(chapter['sources']) for chapter in chapters)
    result['total_code_points'] = sum(len(source['text']) for chapter in chapters for source in chapter['sources'])
    return result


def main():
    path = ROOT / 'corpus.json'
    path.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + '\n')
    print(path)


if __name__ == '__main__':
    main()
