import json
import uuid
from contextlib import closing

from backend.core import db

MAX_BYTES = 20 * 1024 * 1024


def init_schema(connection_factory=db):
    with closing(connection_factory()) as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS chat_attachments(
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            message_id TEXT, name TEXT NOT NULL, media_type TEXT NOT NULL, kind TEXT NOT NULL,
            byte_size INTEGER NOT NULL, data BLOB NOT NULL, chunks_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS attachment_conversation ON chat_attachments(user_id,conversation_id);
        CREATE TABLE IF NOT EXISTS attachment_imports(
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            attachment_id TEXT NOT NULL, knowledge_base_id TEXT NOT NULL,
            request_message_id TEXT NOT NULL, confirmation_message_id TEXT,
            question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', result_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS attachment_model_capabilities(
            model_id TEXT PRIMARY KEY, vision INTEGER NOT NULL DEFAULT 0);
        ''')
        # Run once, preserving later administrator changes to tool bindings.
        version = 'chat_attachment_import_v1'
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='app_migrations'").fetchone():
            if not con.execute('SELECT 1 FROM app_migrations WHERE version=?', (version,)).fetchone():
                for row in con.execute("SELECT id,tools FROM agents WHERE id IN ('general-agent','data-query-agent')").fetchall():
                    tools = json.loads(row['tools'] or '[]')
                    if 'import_attachment_to_knowledge' not in tools:
                        tools.append('import_attachment_to_knowledge')
                    con.execute('UPDATE agents SET tools=? WHERE id=?', (json.dumps(tools), row['id']))
                con.execute('INSERT INTO app_migrations(version,applied_at) VALUES (?,CURRENT_TIMESTAMP)', (version,))
        con.commit()


def owned_conversation(con, conversation_id, user_id):
    if not con.execute('SELECT 1 FROM conversations WHERE id=? AND user_id=?', (conversation_id, user_id)).fetchone():
        raise LookupError('Conversation not found')


def view(row):
    return {key: row[key] for key in ('id', 'name', 'kind', 'media_type', 'byte_size', 'message_id')}


def save(conversation_id, user_id, name, media_type, kind, data, chunks):
    with closing(db()) as con:
        con.execute('BEGIN IMMEDIATE')
        owned_conversation(con, conversation_id, user_id)
        usage = con.execute('SELECT COALESCE(SUM(byte_size),0) FROM chat_attachments WHERE user_id=?', (user_id,)).fetchone()[0]
        count = con.execute('SELECT COUNT(*) FROM chat_attachments WHERE conversation_id=?', (conversation_id,)).fetchone()[0]
        if usage + len(data) > 200 * 1024 * 1024 or count >= 30:
            raise ValueError('附件空间不足（每用户 200 MB、每会话最多 30 个），请删除不再需要的附件。')
        ident = str(uuid.uuid4())
        con.execute('INSERT INTO chat_attachments(id,user_id,conversation_id,name,media_type,kind,byte_size,data,chunks_json) VALUES (?,?,?,?,?,?,?,?,?)',
                    (ident, user_id, conversation_id, name, media_type, kind, len(data), data, json.dumps(chunks, ensure_ascii=False)))
        row = con.execute('SELECT * FROM chat_attachments WHERE id=?', (ident,)).fetchone()
        con.commit()
        return view(row)


def list_files(conversation_id, user_id, *, bound_only=True):
    if not conversation_id or not user_id:
        return []
    with closing(db()) as con:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='chat_attachments'").fetchone():
            return []
        owned_conversation(con, conversation_id, user_id)
        return [view(row) for row in con.execute(
            'SELECT id,name,kind,media_type,byte_size,message_id FROM chat_attachments WHERE conversation_id=? AND user_id=?' +
            (' AND message_id IS NOT NULL' if bound_only else '') + ' ORDER BY created_at,id', (conversation_id, user_id))]


def get_file(ident, conversation_id, user_id):
    with closing(db()) as con:
        owned_conversation(con, conversation_id, user_id)
        row = con.execute('SELECT * FROM chat_attachments WHERE id=? AND conversation_id=? AND user_id=?', (ident, conversation_id, user_id)).fetchone()
        if row is None:
            raise LookupError('Attachment not found')
        return dict(row)


def bind(con, ids, conversation_id, user_id, message_id):
    for ident in ids:
        row = con.execute('SELECT message_id FROM chat_attachments WHERE id=? AND conversation_id=? AND user_id=?', (ident, conversation_id, user_id)).fetchone()
        if row is None or row['message_id'] not in (None, message_id):
            raise LookupError('Attachment not found or already sent')
        con.execute('UPDATE chat_attachments SET message_id=? WHERE id=?', (message_id, ident))


def pending(conversation_id, user_id):
    if not conversation_id or not user_id:
        return None
    with closing(db()) as con:
        row = con.execute("SELECT * FROM attachment_imports WHERE conversation_id=? AND user_id=? AND status='pending' AND created_at > datetime('now','-1 day') ORDER BY created_at DESC LIMIT 1", (conversation_id, user_id)).fetchone()
        return dict(row) if row else None


def propose(conversation_id, user_id, attachment_id, knowledge_base_id, message_id, question):
    with closing(db()) as con:
        con.execute("UPDATE attachment_imports SET status='cancelled' WHERE conversation_id=? AND user_id=? AND status='pending'", (conversation_id, user_id))
        con.execute('INSERT INTO attachment_imports(id,user_id,conversation_id,attachment_id,knowledge_base_id,request_message_id,question) VALUES (?,?,?,?,?,?,?)',
                    (str(uuid.uuid4()), user_id, conversation_id, attachment_id, knowledge_base_id, message_id, question))
        con.commit()


def cancel_pending(conversation_id, user_id):
    with closing(db()) as con:
        con.execute("UPDATE attachment_imports SET status='cancelled' WHERE conversation_id=? AND user_id=? AND status='pending'", (conversation_id, user_id))
        con.commit()
