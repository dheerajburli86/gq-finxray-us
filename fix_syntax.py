import sys; sys.path.insert(0, '.')
content = open('main.py').read()

# Find the broken section and fix it
broken = '''    if source == "ETF_FLOW":
        link = alert.get("link")
        link_line = format_link_line(link, time_str)
        return (
            f"{summary}\n\n"
            f"{link_line}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )'''

fixed = '''    if source == "ETF_FLOW":
        link = alert.get("link")
        link_line = format_link_line(link, time_str) if link else ""
        return (
            f"{summary}\\n\\n"
            f"{link_line}\\n\\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\\n"
            f"_Disclaimer: gquants.com/disclaimer_\\n\\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )'''

content = content.replace(broken, fixed)
open('main.py', 'w').write(content)
print('Fixed syntax')
