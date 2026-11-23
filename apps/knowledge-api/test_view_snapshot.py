import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import field_crypto
import app as api
import test_case_lifecycle as fixtures
from view_snapshot import publish

class ViewSnapshotTests(unittest.TestCase):
    setUp=fixtures.CaseTransitions.setUp
    restore=fixtures.CaseTransitions.restore
    def test_independent_encryption_and_capability_removal(self):
        doc=api._create_document_from_payload({'title':'synthetic','kind':'synthetic','body_md':'SYNTHETIC_BODY'})
        with api._connect() as c:c.execute("UPDATE runs SET claim_token='SYNTHETIC_CAPABILITY',worker_claim_token='SYNTHETIC_WORKER_CAPABILITY'")
        key=field_crypto.load_key();view_key=os.urandom(32);target=Path(self.tmp.name)/'view'/'db.sqlite'
        publish(api.DB_PATH,target,key,view_key)
        c=sqlite3.connect(target);cipher=c.execute('SELECT body_ciphertext FROM documents WHERE id=?',(doc['id'],)).fetchone()[0]
        self.assertEqual(field_crypto.decrypt_text(cipher,key=view_key),'SYNTHETIC_BODY\n')
        with self.assertRaises(Exception):field_crypto.decrypt_text(cipher,key=key)
        self.assertEqual(c.execute('SELECT claim_token,worker_claim_token FROM runs').fetchone(),('',''));c.close()
        # The viewer can read the SQLite bytes, including unused page space.
        # Logical UPDATE alone must not leave the old capabilities recoverable.
        snapshot_bytes = target.read_bytes()
        self.assertNotIn(b'SYNTHETIC_CAPABILITY', snapshot_bytes)
        self.assertNotIn(b'SYNTHETIC_WORKER_CAPABILITY', snapshot_bytes)
        with api._connect() as c:self.assertEqual(c.execute('SELECT claim_token FROM runs').fetchone()[0],'SYNTHETIC_CAPABILITY')
        before=target.read_bytes()
        with self.assertRaises(Exception):publish(api.DB_PATH,target,os.urandom(32),view_key)
        self.assertEqual(target.read_bytes(),before)
        self.assertFalse(list(target.parent.glob('.view-*')))
    def test_same_key_is_rejected(self):
        key=field_crypto.load_key()
        with self.assertRaises(ValueError):publish(api.DB_PATH,Path(self.tmp.name)/'copy.sqlite',key,key)
    def test_legacy_file_content_is_not_silently_lost(self):
        doc=api._create_document_from_payload({'title':'synthetic','kind':'synthetic','body_md':'synthetic'})
        with api._connect() as c:c.execute('UPDATE documents SET body_ciphertext=\'\',path=\'/synthetic/unreviewed\' WHERE id=?',(doc['id'],))
        with self.assertRaises(ValueError):publish(api.DB_PATH,Path(self.tmp.name)/'copy.sqlite',field_crypto.load_key(),os.urandom(32))

    def test_second_automatic_publisher_is_rejected(self):
        import base64
        import knowledge_wsgi
        with patch.dict(os.environ,{'KNOWLEDGE_VIEW_DB':str(Path(self.tmp.name)/'view'/'db.sqlite'),
                                    'KNOWLEDGE_VIEW_KEY':base64.urlsafe_b64encode(os.urandom(32)).decode(),
                                    'KNOWLEDGE_VIEW_KEY_FILE':''}):
            try:
                knowledge_wsgi.application()
                with self.assertRaisesRegex(RuntimeError,'already running'):
                    knowledge_wsgi.application()
            finally:
                api.app.extensions.pop('knowledge_view_lock').close()
