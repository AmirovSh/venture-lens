from pathlib import Path
from alembic.config import Config
from alembic import command
from .settings import ROOT

def upgrade(connection=None):
    cfg=Config(str(ROOT/'alembic.ini'))
    cfg.set_main_option('script_location',str(ROOT/'migrations'))
    if connection is not None:cfg.attributes['connection']=connection
    command.upgrade(cfg,'head')

if __name__=='__main__':
    upgrade();print('Database schema is current')
