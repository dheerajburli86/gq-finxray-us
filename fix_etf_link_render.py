import sys; sys.path.insert(0, '.')
content = open('main.py').read()

# Find the ETF_FLOW handler and add link line
old_etf = '''    if source == "ETF_FLOW":
        # No link line: ETF flow signals are computed, not sourced from a document
        return (
            f"{summary}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"'''

new_etf = '''    if source == "ETF_FLOW":
        link = alert.get("link")
        link_line = format_link_line(link, time_str)
        return (
            f"{summary}\n\n"
            f"{link_line}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"'''

content = content.replace(old_etf, new_etf)
open('main.py', 'w').write(content)
print('Added link rendering to ETF_FLOW alerts')
