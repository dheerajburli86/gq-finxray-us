import sys; sys.path.insert(0, '.')
with open('main.py', 'r') as f:
    lines = f.readlines()
    for i, line in enumerate(lines):
        if 'if source == "ETF_FLOW"' in line:
            for j in range(i, min(i+15, len(lines))):
                print(f'{j+1}: {lines[j].rstrip()}')
            break
