import sys, os
lines = sys.stdin.read().strip().split('\n')
changed_source_files = []
deleted_paths = []
manifest_changes = []

manifest_names = {'package.json', 'pyproject.toml', 'requirements.txt', 'go.mod', 'Cargo.toml', 'composer.json', 'Gemfile', 'pubspec.yaml', 'mix.exs'}
doc_extensions = {'.md', '.json', '.lock', '.yaml', '.yml', '.db', '.db-shm', '.db-wal', '.toml'}

for line in lines:
    if not line: continue
    status, path = line.split('\t', 1)
    
    if os.path.basename(path) in manifest_names:
        manifest_changes.append(path)
        continue
    
    if status == 'D':
        deleted_paths.append(path)
        continue
        
    ext = os.path.splitext(path)[1].lower()
    
    # Filter out docs, tests, etc.
    if ext in doc_extensions: continue
    if 'test_' in path or '_test' in path or 'tests/' in path or '__tests__/' in path: continue
    
    changed_source_files.append((status, path))

print("SOURCE_FILES:")
for s, p in changed_source_files:
    print(f"{s}\t{p}")

print("\nMANIFEST_CHANGES:")
for p in manifest_changes:
    print(p)
    
print("\nDELETED_PATHS:")
for p in deleted_paths:
    print(p)
