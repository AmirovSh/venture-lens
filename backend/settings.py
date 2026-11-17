from pathlib import Path
import os
import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('DATA_DIR',str(ROOT/'data')))
local = {**dotenv_values(ROOT / '.env.local'), **os.environ}
external_path = local.get('CORPORATE_ENV_FILE', '')
external = dotenv_values(external_path) if external_path and Path(external_path).is_file() else {}
env = {**external, **local}

def setting(name, default=''):
    return env.get(name) or default

DATABASE_URL = setting('DATABASE_URL', f'sqlite:///{(DATA / "venture-lens.db").as_posix()}')
BASE_URL = setting('LLM_BASE_URL', setting('SEMANTIC_GRAPH_LLM_BASE_URL')).rstrip('/')
API_KEY = setting('LLM_API_KEY', setting('OPENAI_API_KEY'))
MODEL = setting('LLM_MODEL', 'deepseek-v4-flash')
FALLBACK_MODEL = setting('LLM_FALLBACK_MODEL')
TRUST_ENV = setting('LLM_TRUST_ENV', 'false').lower() == 'true'
SEARCH_TRUST_ENV = setting('SEARCH_TRUST_ENV', 'true').lower() == 'true'

def policy():
    with (ROOT / 'config/policy.yaml').open(encoding='utf-8') as f:
        result = yaml.safe_load(f)
    if abs(sum(x['weight'] for x in result['weights'].values()) - 1) > .0001:
        raise ValueError('Weights must sum to 1')
    return result
