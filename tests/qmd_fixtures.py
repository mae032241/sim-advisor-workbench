"""Small real SQLite QMD keyword stores for orchestration/lifecycle tests."""
import hashlib


def keyword_schema(db):
    db.executescript("""
        PRAGMA auto_vacuum=INCREMENTAL;
        PRAGMA user_version=1;
        CREATE TABLE content(hash TEXT PRIMARY KEY,doc TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE documents(id INTEGER PRIMARY KEY AUTOINCREMENT,collection TEXT NOT NULL,path TEXT NOT NULL,
            title TEXT NOT NULL,hash TEXT NOT NULL,created_at TEXT NOT NULL,modified_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,UNIQUE(collection,path));
        CREATE INDEX idx_documents_hash ON documents(hash);
        CREATE TABLE store_collections(name TEXT PRIMARY KEY,path TEXT NOT NULL,pattern TEXT NOT NULL,context TEXT);
        CREATE TABLE store_config(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE llm_cache(hash TEXT PRIMARY KEY,result TEXT,created_at TEXT);
        CREATE TABLE content_vectors(hash TEXT,seq INTEGER,pos INTEGER,model TEXT,embed_fingerprint TEXT);
        CREATE VIRTUAL TABLE documents_fts USING fts5(filepath,title,body,tokenize='porter unicode61');
        CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
            DELETE FROM documents_fts WHERE rowid = old.id; END;
    """)


def keyword_insert(db, collection, filename, title, body):
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    db.execute('INSERT OR IGNORE INTO store_collections VALUES(?,?,?,NULL)',
               (collection, collection + '/corpus', '**/*.md'))
    db.execute('INSERT OR IGNORE INTO content VALUES(?,?,?)', (digest, body, 'fixture'))
    cursor = db.execute('INSERT INTO documents(collection,path,title,hash,created_at,modified_at) VALUES(?,?,?,?,?,?)',
                        (collection, filename, title, digest, 'fixture', 'fixture'))
    db.execute('INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(?,?,?,?)',
               (cursor.lastrowid, collection + '/' + filename, title, body))
    return digest


def keyword_collection(db, collection):
    """Semantic snapshot including saved content and that collection's FTS rows."""
    return {
        'collection': db.execute('SELECT * FROM store_collections WHERE name=?', (collection,)).fetchall(),
        'documents': db.execute('SELECT * FROM documents WHERE collection=? ORDER BY id', (collection,)).fetchall(),
        'content': db.execute('SELECT c.* FROM content c WHERE hash IN (SELECT hash FROM documents WHERE collection=?) ORDER BY hash',
                              (collection,)).fetchall(),
        'fts': db.execute('SELECT f.rowid,f.* FROM documents_fts f WHERE rowid IN (SELECT id FROM documents WHERE collection=?) ORDER BY rowid',
                          (collection,)).fetchall(),
    }
