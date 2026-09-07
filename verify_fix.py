import sys; sys.path.insert(0, '.')
with open('main.py', 'r') as f:
    content = f.read()
    
if 'if source == "ETF_FLOW":' in content:
    start = content.find('if source == "ETF_FLOW":')
    section = content[start:start+500]
    print(section)
else:
    print('ETF_FLOW handler not found')
