"""Publish an atomic read-only database with an independent encryption key."""
import os
from pathlib import Path
import sqlite3
import tempfile
import field_crypto


def publish(source, destination, source_key, view_key):
    if source_key == view_key:
        raise ValueError('view encryption key must be independent')
    destination = Path(destination)
    if Path(source).resolve() == destination.resolve():
        raise ValueError('source and view must differ')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.view-', dir=destination.parent)
    os.close(fd)
    src = dst = None
    try:
        src = sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro', uri=True)
        dst = sqlite3.connect(name)
        src.backup(dst)
        # The reader has access to database bytes, not just SQL rows. Scrub
        # replaced capabilities and rebuild free pages before making it visible.
        dst.execute('PRAGMA secure_delete=ON')
        for (table,) in dst.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            quoted = '"'+table.replace('"','""')+'"'
            columns = [r[1] for r in dst.execute('PRAGMA table_info('+quoted+')')]
            encrypted = [c for c in columns if c.endswith('_ciphertext')]
            for row in dst.execute('SELECT rowid,* FROM '+quoted).fetchall():
                values = dict(zip(['rowid',*columns], row)); updates = {}
                for col in encrypted:
                    if values[col]:
                        updates[col] = field_crypto.encrypt_text(field_crypto.decrypt_text(values[col], key=source_key), key=view_key)
                for col in ['claim_token','worker_claim_token']:
                    if col in values:updates[col] = ''
                if table == 'documents' and values.get('path'):
                    if not values.get('body_ciphertext'):
                        raise ValueError('legacy file documents require reviewed migration')
                    updates['path'] = ''
                if updates:
                    keys = list(updates)
                    dst.execute('UPDATE '+quoted+' SET '+','.join('"'+k+'"=?' for k in keys)+' WHERE rowid=?', [*[updates[k] for k in keys],values['rowid']])
        dst.commit()
        dst.execute('VACUUM')
        if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('invalid view snapshot')
        dst.close();dst=None;src.close();src=None
        with open(name,'rb') as file:os.fsync(file.fileno())
        os.chmod(name,0o440)
        os.replace(name,destination)
    finally:
        if dst is not None:dst.close()
        if src is not None:src.close()
        try:os.unlink(name)
        except FileNotFoundError:pass
