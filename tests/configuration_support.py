"""Explicit operator-only synthetic setup. Never a runtime API bypass or login grant."""
from contextlib import contextmanager
from tools.dev_db import connection

@contextmanager
def configuration_connection(database,*,autocommit=True):
 with connection('lv_migrator',database,autocommit=autocommit) as c:
  c.execute('SET ROLE lv_owner')
  yield c
