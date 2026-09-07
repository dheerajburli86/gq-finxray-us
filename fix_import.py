import sys; sys.path.insert(0, '.')
content = open('main.py').read()
content = content.replace(
    'from heatmap_generator import run_sector_heatmap_daily, run_sector_heatmap_weekly, run_sector_heatmap_monthly',
    'from heatmap_generator import run_sector_heatmap_midday, run_sector_heatmap_afternoon, run_sector_heatmap_weekly, run_sector_heatmap_monthly'
)
open('main.py', 'w').write(content)
print('Fixed import')
