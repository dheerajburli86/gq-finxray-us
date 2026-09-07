import sys; sys.path.insert(0, '.')
content = open('main.py').read()

old = '''    if source == "ETF_FLOW":
        link = alert.get("link")
        link_line = format_link_line(link, time_str) if link else ""
        return (
            f"{summary}\n\n"
            f"{link_line}\n\n"'''

new = '''    if source == "ETF_FLOW":
        return (
            f"{summary}\n\n"'''

content = content.replace(old, new)
open('main.py', 'w').write(content)
print('Reverted main.py')
