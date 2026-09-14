import base64
import io
import json
import re
from pathlib import Path
from contextlib import closing

from backend.core import db
from backend.knowledge.service import parse_and_chunk_document
from backend.knowledge.models import DocumentIngestionError
from backend.attachments import repository as repo

IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.webp'}


def upload(conversation_id, user_id, name, media_type, data):
    name = name.replace('\\', '/').rsplit('/', 1)[-1][:180]
    if not data or len(data) > repo.MAX_BYTES:
        raise ValueError('附件不能为空，且不能超过 20 MB。')
    if Path(name).suffix.lower() in IMAGE_TYPES:
        from PIL import Image, ImageOps
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError('图片像素过多，请缩小后重试。')
                image = ImageOps.exif_transpose(image).convert('RGB')
                image.thumbnail((2048, 2048))
                output = io.BytesIO()
                image.save(output, format='JPEG', quality=90)
                data = output.getvalue()
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError('图片无法解码或尺寸过大。') from exc
        return repo.save(conversation_id, user_id, name, 'image/jpeg', 'image', data, [])
    result = parse_and_chunk_document(name, media_type, data)
    chunks = [{'content': chunk.content, 'locator': dict(chunk.locator)} for chunk in result.chunks]
    return repo.save(conversation_id, user_id, name, media_type or 'application/octet-stream', 'document', data, chunks)


def document_context(files, question, budget=18000):
    """Bounded local lexical retrieval; never send an entire large file implicitly."""
    terms = set(re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]', question.lower()))
    candidates = []
    for file in files:
        for index, chunk in enumerate(json.loads(file['chunks_json'])):
            content = chunk['content']
            score = sum(term in content.lower() for term in terms)
            candidates.append((score, index, file['name'], chunk))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    selected, used = [], 0
    for _, index, name, chunk in candidates:
        text = f"文件 {name}，片段 {index + 1}，位置 {json.dumps(chunk['locator'], ensure_ascii=False)}\n{chunk['content']}"
        if used + len(text) > budget:
            continue
        selected.append(text)
        used += len(text)
    return '\n\n'.join(selected), len(selected), len(candidates)


def vision_models():
    with closing(db()) as con:
        return [dict(row) for row in con.execute('''SELECT m.id,m.display_name,p.kind,
            COALESCE(c.vision,0) AS vision FROM models m JOIN providers p ON p.id=m.provider_id
            LEFT JOIN attachment_model_capabilities c ON c.model_id=m.id
            WHERE m.enabled=1 AND p.enabled=1 ORDER BY m.id''')]


def image_part(file):
    return {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(file['data']).decode()}}
