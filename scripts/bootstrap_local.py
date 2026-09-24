"""Copy public templates to ignored local config, without replacing existing files."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'config' / 'examples'
PAIRS = [
    ('execution.yaml', 'config/execution.yaml'),
    ('env.example', '.env'),
    ('brain.yaml', 'config/brain.yaml'),
    ('slaver.yaml', 'config/slaver.yaml'),
    ('robot_api.yaml', 'config/robot_api.yaml'),
    ('serve_real.yaml', 'config/serve_real.yaml'),
    ('serve_dream.yaml', 'config/dream.yaml'),
    ('dream_navigation_sop.yaml', 'config/dream_navigation_sop.yaml'),
]

def main():
    for source, target in PAIRS:
        src, dst = EXAMPLES/source, ROOT/target
        if dst.exists():
            print(f'KEEP {target}')
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        with src.open('rb') as input_file, dst.open('xb') as output:
            shutil.copyfileobj(input_file, output)
        print(f'CREATED {target}')
    print('No services started. Real actions remain disabled; fill local credentials separately.')

if __name__ == '__main__':
    main()
