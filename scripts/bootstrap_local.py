"""Copy public templates to ignored local config, without replacing existing files."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
PAIRS = [('.env.example', '.env')]
PAIRS += [(f'{name}/config.example.yaml', f'{name}/config.yaml') for name in
          ('master','slaver','robot_api','serve_real','serve_dream')]
PAIRS += [('serve_dream/dream_navigation_sop.example.yaml', 'serve_dream/dream_navigation_sop.yaml')]

def main():
    for source, target in PAIRS:
        src, dst = ROOT/source, ROOT/target
        if dst.exists():
            print(f'KEEP {target}')
            continue
        with dst.open('xb') as output, src.open('rb') as input_file:
            shutil.copyfileobj(input_file, output)
        print(f'CREATED {target}')
    print('No services started. Real actions remain disabled; fill local credentials separately.')

if __name__ == '__main__':
    main()
