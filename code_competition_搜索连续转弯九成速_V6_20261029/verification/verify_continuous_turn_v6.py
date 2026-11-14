from pathlib import Path
import hashlib,json,re,xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'verification/continuous_turn_v6_20261029';USER=ROOT/'USER'
baseline=json.loads((OUT/'source_copy_baseline.json').read_text(encoding='utf-8'))
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
for key in ('v5','v3','original'):
    source=Path(baseline[key+'_root'])
    current={str(p.relative_to(source)):digest(p) for p in source.rglob('*') if p.is_file()}
    assert current==baseline[key+'_files'],key+' source changed'
recipe=json.loads((OUT/'patch_recipe.json').read_text(encoding='utf-8'))
assert {r['file'] for r in recipe}=={'UTILITY/utility.c'}
data=(ROOT/'UTILITY/utility.c').read_bytes();data.decode('cp936')
assert data.count(b'\r\n')==data.count(b'\n')
restored=data
for item in reversed(recipe):
    a=item['new'].replace('\n','\r\n').encode('ascii');b=item['old'].replace('\n','\r\n').encode('ascii')
    assert restored.count(a)==1,item['new'][:80];restored=restored.replace(a,b,1)
assert hashlib.sha256(restored).hexdigest()==baseline['v5_files'][str(Path('UTILITY/utility.c'))]
for rel,h in baseline['v5_files'].items():
    p=Path(rel)
    if p.parts[0]=='verification' or p.suffix.lower() not in ('.c','.h','.s'):continue
    if p.as_posix()!='UTILITY/utility.c':assert digest(ROOT/p)==h,rel
assert (ROOT/'UTILITY/search_turn_v6.h').is_file()
def body(b,n):
    m=re.search(rb'^(?:static\s+)?(?:void|u8)\s+'+n.encode()+rb'\s*\([^;]*?\)\s*\{',b,re.M)
    assert m,n;i=b.index(b'{',m.start());j=i+1;level=1
    while level:
        if b[j]==123:level+=1
        if b[j]==125:level-=1
        j+=1
    return b[m.start():j]
v5=(Path(baseline['v5_root'])/'UTILITY/utility.c').read_bytes()
protected=('taskturn','before90turn','mouseGoahead','mazeSearch','goalDepartureAfterTurnback',
           'before180turn','after180turn','v5CaptureStop','v5FrontRecheck','v5PrintStop',
           'spurtSearchUturn','gridSearchParameters','spurt_grid90','__mouseCoorUpdate')
for name in protected:assert body(data,name)==body(v5,name),name
for name in ('mouseTurnright','mouseTurnleft'):
    f=body(data,name)
    assert f.index(b'v6UseSlowSearchTurn()')<f.index(b'LastSearchAction=')<f.index(b'v6SlowSearchTurn90(')
assert b'#define V6_TURN_CHAIN v5_diag.reserved' in data
assert b'V6_TURN_CHAIN==1' in body(data,'v6UseSlowSearchTurn')
for name in ('mazesearchhaswall','mazesearchnowall','mouseTurnback','specialmouseTurnback'):
    assert b'V6_TURN_CHAIN=0;' in body(data,name)
assert b'V6_TURN_CHAIN=0;' not in body(data,'mouseGoahead')
project=ET.parse(USER/'TEST.uvprojx').getroot();targets=project.findall('./Targets/Target')
names=['V6_%dmm'%n for n in (0,2,4,6)]
assert [t.findtext('TargetName') for t in targets]==names
for t,n in zip(targets,(0,2,4,6)):
    d=t.findtext('./TargetOption/TargetArmAds/Cads/VariousControls/Define')
    assert 'MOUSE_REMOTE' not in d and 'GOAL_DEPARTURE_EXTRA_MM=%d'%n in d
    assert t.findtext('./TargetOption/TargetCommonOption/OutputName')=='TEST_V6_%dmm'%n
    assert (USER/'RTE'/('_V6_%dmm'%n)/'RTE_Components.h').is_file()
opts=ET.parse(USER/'TEST.uvoptx').getroot().findall('Target')
assert [o.findtext('TargetName') for o in opts]==names
assert [o.findtext('./TargetOption/OPTFL/IsCurrentTarget') for o in opts]==['0','0','1','0']
def image(p):
    data={};base=0;eof=False
    for line in p.read_text().splitlines():
        assert line.startswith(':');r=bytes.fromhex(line[1:]);assert len(r)==r[0]+5 and sum(r)%256==0
        n=r[0];addr=int.from_bytes(r[1:3],'big');typ=r[3]
        if typ==0:
            for i,b in enumerate(r[4:4+n]):assert base+addr+i not in data;data[base+addr+i]=b
        elif typ==4:base=int.from_bytes(r[4:6],'big')<<16
        elif typ==2:base=int.from_bytes(r[4:6],'big')<<4
        elif typ==1:eof=True
        else:assert typ in (3,5)
    assert eof and data;return data
builds=json.loads((OUT/'keil_build_results.json').read_text(encoding='utf-8'))
assert [b['target'] for b in builds]==names
old_log=Path(baseline['v5_root'])/'verification/front_recheck_v5_20261029/keil_V5_4mm.log'
oldwarnings=sorted(re.findall(r'warning:\s+#([\w-]+)',old_log.read_bytes().decode('mbcs',errors='replace'),re.I))
images=[]
for b in builds:
    n=b['extra_mm'];p=ROOT/b['hex'];rom=image(p)
    assert min(rom)==0x08000000 and max(rom)<0x08016000
    assert b['errors']==0 and b['warnings']==37 and digest(p)==b['sha256']
    size=list(map(int,re.search(r'Code=(\d+) RO-data=(\d+) RW-data=(\d+) ZI-data=(\d+)',b['size']).groups()))
    assert size[2:]==[4960,15520]
    log=(OUT/('keil_'+b['target']+'.log')).read_bytes().decode('mbcs',errors='replace')
    assert sorted(re.findall(r'warning:\s+#([\w-]+)',log,re.I))==oldwarnings
    delivered=ROOT/'固件_V6_连续转弯九成速_20261029'/('TEST_V6_连弯九成速_终点附加%dmm_20261029'%n)
    for ext in ('hex','axf'):assert delivered.with_suffix('.'+ext).read_bytes()==p.with_suffix('.'+ext).read_bytes()
    maptext=p.with_suffix('.map').read_text(encoding='mbcs',errors='replace')
    assert re.search(r'v5_diag\s+0x[0-9a-f]+\s+Data\s+256\s',maptext,re.I)
    assert re.search(r'run_data\s+0x[0-9a-f]+\s+Data\s+9088\s',maptext,re.I)
    htm=p.with_suffix('.htm').read_text(encoding='mbcs',errors='replace')
    def depth(name):return int(re.search(r'</a>'+name+r'</STRONG>.*?Max Depth = (\d+)',htm,re.S)[1])
    main_frame=int(re.search(r'</a>main</STRONG>[^\n]*Stack size (\d+)',htm)[1])
    search_stack=main_frame+depth('objectGoTo')+depth('SysTick_Handler')+32
    startup_stack=depth('main')+depth('SysTick_Handler')+32
    assert search_stack<768 and startup_stack<768
    images.append({'target':b['target'],'last_flash_byte':hex(max(rom)),'sha256':b['sha256'],'ram_bytes':sum(size[2:]),'search_stack_estimate':search_stack,'startup_stack_estimate':startup_stack,'slow_turn_stack':depth('v6SlowSearchTurn90')})
assert len({b['sha256'] for b in builds})==4
for ext in ('hex','axf'):
    assert (USER/('TEST.'+ext)).read_bytes()==(ROOT/builds[2]['hex']).with_suffix('.'+ext).read_bytes()
turn=json.loads((OUT/'continuous_turn_test_results.json').read_text(encoding='utf-8'))
goal=json.loads((OUT/'turn_and_goal_host/goal_departure_test_results.json').read_text(encoding='utf-8'))
guard=json.loads((OUT/'guard_test_results.json').read_text(encoding='utf-8'))
grid=json.loads((ROOT/'verification/grid90_transition/test_results.json').read_text(encoding='utf-8'))
assert turn['status']==goal['status']==guard['status']==grid['status']=='PASS'
assert turn['goal_regression_checks']==goal['checks']
grid_checks=sum(c['passed'] for c in grid['cases'])
report={'status':'PASS','v5_files_unchanged':len(baseline['v5_files']),'v3_files_unchanged':len(baseline['v3_files']),
 'original_files_unchanged':len(baseline['original_files']),'default':'V6_4mm','byte_exact_inverse_patch':True,
 'encoding_crlf_preserved':True,'ram_bytes':20480,'stack_bytes':768,'new_global_ram':0,
 'changed_production_files':['UTILITY/utility.c'],'new_internal_header':'UTILITY/search_turn_v6.h',
 'protected_functions_byte_identical':list(protected),'software_checks':goal['checks']+turn['checks']+guard['checks']+grid_checks,
 'goal_checks':goal['checks'],'continuous_checks':turn['checks'],'guard_checks':guard['checks'],'sprint_checks':grid_checks,
 'geometry':turn['geometry'],'images':images}
(OUT/'delivery_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:report[k] for k in ('status','v5_files_unchanged','default','software_checks','ram_bytes','new_global_ram')}))
