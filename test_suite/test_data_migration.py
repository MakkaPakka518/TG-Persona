"""Encrypted whole-panel migration packages; temporary files only."""
from pathlib import Path
import json
import shutil
import sqlite3
import tempfile
import unittest

import data_migration as migration


def database(path, statements):
    db=sqlite3.connect(path)
    try:
        for sql,args in statements: db.execute(sql,args)
        db.commit()
    finally: db.close()


class DataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='tg-migration-');self.base=Path(self.tmp.name);self.data=self.base/'data';self.data.mkdir()
        self.aid='a'*32
        database(self.data/'config.db',[("CREATE TABLE accounts(id TEXT PRIMARY KEY,label TEXT,created_at INTEGER)",()),("INSERT INTO accounts VALUES(?,?,?)",('default','默认',1)),("INSERT INTO accounts VALUES(?,?,?)",(self.aid,'小号',2)),("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)",()),("INSERT INTO settings VALUES(?,?)",('ai_key','SECRET-FIXTURE'))])
        directory=self.data/'accounts'/self.aid;directory.mkdir(parents=True)
        database(directory/'config.db',[("CREATE TABLE automations(id INTEGER PRIMARY KEY,name TEXT)",()),("INSERT INTO automations VALUES(?,?)",(1,'任务'))])
        database(self.data/'tg.session',[("CREATE TABLE sessions(dc_id INTEGER)",()),("INSERT INTO sessions VALUES(?)",(1,))])
        database(directory/'tg.session',[("CREATE TABLE sessions(dc_id INTEGER)",()),("INSERT INTO sessions VALUES(?)",(2,))])
        (self.data/'avatar.jpg').write_bytes(b'avatar')
        (self.data/'skill.md').write_text('- root style')
        (directory/'skill.md').write_text('- account style')

    def tearDown(self): self.tmp.cleanup()

    def test_encrypted_round_trip_includes_all_accounts_and_secrets(self):
        package,export_work=migration.create_package(self.base,self.data,[self.aid],'1.16.1','backup-password')
        self.assertTrue(package.read_bytes().startswith(migration.MAGIC));self.assertNotIn(b'SECRET-FIXTURE',package.read_bytes())
        with self.assertRaisesRegex(ValueError,'密码错误'):
            migration.stage_package(self.base,package,'wrong-password','1.16.1')
        restore_id,preview,stage=migration.stage_package(self.base,package,'backup-password','1.16.1')
        self.assertEqual((preview['accounts'],preview['files']),(2,7));self.assertTrue((stage/'new/data/accounts'/self.aid/'tg.session').is_file());self.assertTrue((stage/'new/data/accounts'/self.aid/'skill.md').is_file())
        tampered=self.base/'tampered.tgp';changed=bytearray(package.read_bytes());changed[-20]^=1;tampered.write_bytes(changed)
        with self.assertRaisesRegex(ValueError,'密码错误|损坏'): migration.stage_package(self.base,tampered,'backup-password','1.16.1')
        self.assertFalse(migration._allowed('../data/config.db'));self.assertFalse(migration._allowed('data/accounts/bad/config.db'))
        shutil.rmtree(export_work);shutil.rmtree(stage)

    def test_skill_one_mib_migration_boundary(self):
        skill=self.data/'skill.md';skill.write_bytes(b'A'*migration.SKILL_MAX_BYTES)
        package,work=migration.create_package(self.base,self.data,[self.aid],'1.16.1','backup-password')
        _,_,stage=migration.stage_package(self.base,package,'backup-password','1.16.1')
        self.assertEqual((stage/'new/data/skill.md').stat().st_size,migration.SKILL_MAX_BYTES)
        shutil.rmtree(work);shutil.rmtree(stage)
        skill.write_bytes(b'A'*(migration.SKILL_MAX_BYTES+1))
        with self.assertRaisesRegex(ValueError,'1MiB'):migration.create_package(self.base,self.data,[self.aid],'1.16.1','backup-password')

    def test_startup_swap_success_and_failed_boot_rollback(self):
        package,export_work=migration.create_package(self.base,self.data,[self.aid],'1.16.1','backup-password')
        restore_id,_,stage=migration.stage_package(self.base,package,'backup-password','1.16.1')
        (self.data/'current-marker').write_text('old');migration.arm_restore(self.base,restore_id,stage)
        self.assertEqual(migration.prepare_startup_restore(self.base),'booting');self.assertFalse((self.data/'current-marker').exists())
        self.assertTrue((self.base/('.data-pre-restore-'+restore_id)/'current-marker').is_file())
        migration.finish_startup_restore(self.base);self.assertFalse((self.base/'.migration-restore.json').exists())
        shutil.rmtree(export_work,ignore_errors=True)

    def test_second_boot_before_success_rolls_back(self):
        package,export_work=migration.create_package(self.base,self.data,[self.aid],'1.16.1','backup-password')
        restore_id,_,stage=migration.stage_package(self.base,package,'backup-password','1.16.1')
        (self.data/'current-marker').write_text('old');migration.arm_restore(self.base,restore_id,stage)
        migration.prepare_startup_restore(self.base)
        self.assertEqual(migration.prepare_startup_restore(self.base),'rolled_back');self.assertTrue((self.data/'current-marker').is_file())
        migration.finish_startup_restore(self.base);self.assertFalse((self.base/'.migration-restore.json').exists());self.assertFalse(stage.exists());shutil.rmtree(export_work,ignore_errors=True)

    def test_interrupted_swap_and_rollback_resume_safely(self):
        package,export_work=migration.create_package(self.base,self.data,[self.aid],'1.16.1','backup-password')
        restore_id,_,stage=migration.stage_package(self.base,package,'backup-password','1.16.1');migration.arm_restore(self.base,restore_id,stage)
        marker=self.base/'.migration-restore.json';old=self.base/('.data-pre-restore-'+restore_id);failed=self.base/('.data-failed-'+restore_id)
        migration._atomic_json(marker,{'phase':'swapping','id':restore_id,'stage':stage.name});(self.data/'current-marker').write_text('old');self.data.replace(old)
        self.assertEqual(migration.prepare_startup_restore(self.base),'booting');self.assertFalse((self.data/'current-marker').exists())
        migration._atomic_json(marker,{'phase':'rolling_back','id':restore_id,'stage':stage.name});self.data.replace(failed);old.replace(self.data)
        self.assertEqual(migration.prepare_startup_restore(self.base),'rolled_back');self.assertTrue((self.data/'current-marker').is_file())
        self.assertEqual(migration.prepare_startup_restore(self.base),'rolled_back');self.assertFalse(failed.exists());self.assertFalse(stage.exists());shutil.rmtree(export_work,ignore_errors=True)


if __name__=='__main__':unittest.main()
