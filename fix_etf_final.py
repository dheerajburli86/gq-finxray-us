import sys; sys.path.insert(0, '.')
content = open('main.py').read()

# Find and replace the entire ETF_FLOW section
start_marker = '    if source == "ETF_FLOW":'
end_marker = '    if source == "SECTOR_HEATMAP":'

start_idx = content.find(start_marker)
end_idx = content.find(end_marker)

if start_idx != -1 and end_idx != -1:
    new_section = '''    if source == "ETF_FLOW":
        link = alert.get("link")
        link_line = format_link_line(link, time_str)
        return (
            f"{summary}\n\n"
            f"{link_line}\n\n"
            f"_You are receiving this notification based on your request to monitor this stock's news, updates and transactions._\n"
            f"_Disclaimer: gquants.com/disclaimer_\n\n"
            f"📊 Manage your AI-powered watchlist: https://gquants.com/build"
            f"{footer}"
        )

'''
    content = content[:start_idx] + new_section + content[end_idx:]
    open('main.py', 'w').write(content)
    print('✓ Fixed ETF_FLOW link rendering')
else:
    print('ERROR: Could not find markers')
