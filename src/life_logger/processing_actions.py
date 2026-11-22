"""Explicit processing commands, kept out of HTTP request threads."""
from __future__ import annotations
import json


def natural_correction(host, payload):
    from life_logger.intent_pool import ActiveIntentPool
    from life_logger.natural_correction import DeepSeekTriIntentParser, process_natural_correction
    from life_logger.local_pipeline import local_pipeline_env
    parser = DeepSeekTriIntentParser(api_key='')
    try:
        config = json.loads(host.config_path.read_text(encoding='utf-8'))
        if not isinstance(config, dict):
            config = {}
        try:
            secrets = json.loads((host.store.root/'secrets.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            secrets = {}
        if isinstance(secrets, dict) and secrets.get('deepseek_api_key'):
            config['_deepseek_api_key'] = str(secrets['deepseek_api_key'])
            if not isinstance(config.get('deepseek'), dict):
                config['deepseek'] = {}
            config['deepseek']['enabled'] = True
        env = local_pipeline_env(config)
        key = str(env.get('DEEPSEEK_API_KEY') or '').strip()
        if key:
            options = {'api_key': key}
            if env.get('DEEPSEEK_BASE_URL'):
                options['base_url'] = str(env['DEEPSEEK_BASE_URL'])
            if env.get('DEEPSEEK_MODEL'):
                options['model'] = str(env['DEEPSEEK_MODEL'])
            parser = DeepSeekTriIntentParser(**options)
    except Exception:
        parser = DeepSeekTriIntentParser(api_key='')
    result = process_natural_correction(host.store, text=str(payload.get('text') or ''),
        target_date=payload.get('date'), job_id=payload.get('job_id'),
        artifact_path=payload.get('artifact_path'), target_event_id=payload.get('target_event_id'),
        intent_pool=ActiveIntentPool(), parser=parser)
    if result.get('status') == 'ok':
        return result
    return {'__processing_http_status__': 400, '__processing_body__': result}
