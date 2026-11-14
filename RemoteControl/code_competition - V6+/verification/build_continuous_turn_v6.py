"""Rebuild the four selectable Keil targets; no flashing is performed."""
from pathlib import Path
import hashlib
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
USER=ROOT/'USER'
OUT=ROOT/'verification/continuous_turn_v6_20261029'
DELIVERY=ROOT/'固件_V6_连续转弯九成速_20261029'
UV4=Path(r'D:\Keil5MDK\UV4\UV4.exe')
OUT.mkdir(exist_ok=True)
DELIVERY.mkdir(exist_ok=True)
targets=ET.parse(USER/'TEST.uvprojx').getroot().findall('./Targets/Target')
assert [t.findtext('TargetName') for t in targets]==['V6_%dmm'%n for n in (0,2,4,6)]
results=[]
startup=subprocess.STARTUPINFO()
startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
startup.wShowWindow=subprocess.SW_HIDE
for extra,target in zip((0,2,4,6),targets):
    name=target.findtext('TargetName')
    defs=target.findtext('./TargetOption/TargetArmAds/Cads/VariousControls/Define').split(',')
    assert [d.strip() for d in defs if d.strip().startswith('GOAL_DEPARTURE_EXTRA_MM')]==['GOAL_DEPARTURE_EXTRA_MM=%d'%extra]
    assert 'MOUSE_REMOTE' not in defs
    common=target.find('./TargetOption/TargetCommonOption')
    work=(USER/common.findtext('OutputDirectory')).resolve()
    assert work==USER/'Build'/name
    stem=common.findtext('OutputName')
    assert stem=='TEST_'+name
    log=OUT/('keil_'+name+'.log')
    command=[str(UV4),'-r',str(USER/'TEST.uvprojx'),'-t',name,'-j0','-o',str(log)]
    print('Keil Rebuild: '+name,flush=True)
    try:
        p=subprocess.run(command,cwd=USER,capture_output=True,timeout=55,startupinfo=startup,creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Keil timed out rebuilding '+name)
    output=log.read_bytes().decode('mbcs',errors='replace')
    count=re.search(r' - (\d+) Error\(s\), (\d+) Warning\(s\)',output)
    size=re.search(r'Program Size:.*',output)
    if p.returncode not in (0,1) or not count or count[1]!='0' or not size:
        raise RuntimeError(output[-6000:])
    assert (work/(stem+'.hex')).is_file() and (work/(stem+'.axf')).is_file()
    legacy='TEST_V6_连弯九成速_终点附加%dmm_20261029'%extra
    for ext in ('hex','axf'):
        shutil.copy2(work/(stem+'.'+ext),DELIVERY/(legacy+'.'+ext))
    results.append({'target':name,'extra_mm':extra,'returncode':p.returncode,'errors':int(count[1]),'warnings':int(count[2]),
                    'size':size[0].strip(),'command':command,'hex':str((work/(stem+'.hex')).relative_to(ROOT)),
                    'axf':str((work/(stem+'.axf')).relative_to(ROOT)),
                    'sha256':hashlib.sha256((work/(stem+'.hex')).read_bytes()).hexdigest()})
    print(name+': '+size[0].strip()+'; '+count[0].strip(),flush=True)
# Keep the legacy output and selected Keil target on 4 mm.
for ext in ('axf','hex','map','htm'):
    shutil.copy2(USER/'Build/V6_4mm'/('TEST_V6_4mm.'+ext),USER/('TEST.'+ext))
p=USER/'TEST.uvoptx';text=p.read_bytes().decode('utf-8')
def select(m):
    body=m[0];current='1' if '<TargetName>V6_4mm</TargetName>' in body else '0'
    return re.sub(r'<IsCurrentTarget>\d+</IsCurrentTarget>','<IsCurrentTarget>'+current+'</IsCurrentTarget>',body,count=1)
text=re.sub(r'  <Target>\r?\n.*?  </Target>',select,text,flags=re.S)
p.write_bytes(text.encode('utf-8'))
(OUT/'keil_build_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
(DELIVERY/'SHA256SUMS.txt').write_text('\n'.join(r['sha256']+'  TEST_V6_连弯九成速_终点附加%dmm_20261029.hex'%r['extra_mm'] for r in results)+'\n',encoding='utf-8-sig')
print('PASS: all four targets rebuilt by Keil. Default V6_4mm. No flashing.',flush=True)
