"""Run the API locally against the development database as the restricted lv_app login.

  .venv/bin/python -m tools.serve_api              secure cookies (__Host-lv_session, Secure): use behind local HTTPS
  .venv/bin/python -m tools.serve_api --local-http plain-HTTP development cookies (lv_session_local, not Secure),
                                                   only on 127.0.0.1 and only with LV_ENVIRONMENT=development

The database password is read from .env.roles and passed in-process; it is never printed or logged.
"""
import argparse
import os

from psycopg.conninfo import make_conninfo

from tools.dev_db import DEV_DB, env_file


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--local-http', action='store_true', help='explicitly use development cookies over plain HTTP on loopback')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    os.environ['LV_DATABASE_URL'] = make_conninfo(host='127.0.0.1', port=55432, dbname=DEV_DB, user='lv_app',
                                                  password=env_file('.env.roles')['APP_PASSWORD'])
    if args.local_http:
        os.environ['LV_ENVIRONMENT'] = 'development'
        os.environ['LV_COOKIE_MODE'] = 'local-http'
    import uvicorn
    uvicorn.run('ledgervigil.app:app', host='127.0.0.1', port=args.port, access_log=True, proxy_headers=False)


if __name__ == '__main__':
    main()
