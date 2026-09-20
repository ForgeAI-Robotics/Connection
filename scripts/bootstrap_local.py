"""Copy public templates to ignored local config, without replacing existing files."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'config' / 'examples'
PAIRS = [
    ('env.example', '.env'),
    ('master.yaml', 'master/config.yaml'),
    ('slaver.yaml', 'slaver/config.yaml'),
    ('robot_api.yaml', 'robot_api/config.yaml'),
    ('serve_real.yaml', 'extensions/serve_real/config.yaml'),
    ('serve_dream.yaml', 'serve_dream/config.yaml'),
    ('dream_navigation_sop.yaml', 'serve_dream/dream_navigation_sop.yaml'),
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
