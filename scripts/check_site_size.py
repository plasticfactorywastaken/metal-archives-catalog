from pathlib import Path
root=Path('site')
files=[p for p in root.rglob('*') if p.is_file()]
total=sum(p.stat().st_size for p in files)
if total > 900_000_000:
    raise SystemExit('Pages budget exceeded. State snapshot retained; previous live site remains unchanged. Reduce the explicitly published data scope or choose another genuinely free data host.')
print(f'Pages payload: {total} bytes (900 MB safety budget)')
