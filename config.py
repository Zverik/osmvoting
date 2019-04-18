import os
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

DEBUG = False

DATABASE_URI = 'sqlite:///' + os.path.join(BASE_DIR, 'osmvoting.db')
# DATABASE_URI = 'postgresql://localhost/osmvoting'

NOMINATIONS = ['core', 'innovation', 'writing', 'mapping', 'community', 'team', 'ulf']
STAGE = 'call'  # call -> callvote -> select -> voting -> processing -> results
ADMINS = set([290271])  # Zverik
MAX_NOMINEES_PER_USER = 30

# Override these (and anything else) in config_local.py
OAUTH_KEY = ''
OAUTH_SECRET = ''
SECRET_KEY = ''
TEAM = set(ADMINS)  # Who selects the nominees for voting

try:
    from config_local import *
except ImportError:
    pass
