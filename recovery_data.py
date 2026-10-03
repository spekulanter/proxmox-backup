"""Disaster recovery metadáta, wiki a checklist pre Proxmox Backup Manager.

Tento modul obsahuje iba dáta (žiadnu logiku zálohy ani obnovy). `app.py` ich
pripája k backup položkám v API odpovediach; do `backup_config.json` sa neukladajú.

Klasifikácia obnovy (`restore_category`) je nezávislá od existujúcich tagov
critical/recommended/optional: tagy hovoria, ako dôležité je položku ZÁLOHOVAŤ,
restore kategória hovorí, ako bezpečné je ju neskôr OBNOVIŤ na nový hardvér.
"""

# ---------------------------------------------------------------------------
# Restore kategórie
# ---------------------------------------------------------------------------

RESTORE_CATEGORIES = [
    {
        'id': 'required',
        'label': 'Potrebné pre obnovu',
        'badge': 'NEW HW REQUIRED',
        'icon': '🟢',
        'description': 'Kritické dáta pre rekonštrukciu pôvodného Proxmox hosta. Bez nich nový server nebude tým starým.',
    },
    {
        'id': 'review',
        'label': 'Pred obnovou skontrolovať',
        'badge': 'REVIEW FIRST',
        'icon': '🟠',
        'description': 'Dôležitá konfigurácia, ktorá je často viazaná na hardvér. Na inom HW ju najprv porovnaj, neprepisuj naslepo.',
    },
    {
        'id': 'selective',
        'label': 'Obnoviť selektívne',
        'badge': 'SELECTIVE',
        'icon': '🔵',
        'description': 'Zo zálohy obnov iba vlastné položky (skripty, služby, cron joby), nie celý adresár.',
    },
    {
        'id': 'reference',
        'label': 'Použiť ako referenciu',
        'badge': 'REFERENCE ONLY',
        'icon': '⚪',
        'description': 'Na nový systém sa priamo neprepisuje. Slúži ako zdroj informácií pri ručnej rekonštrukcii nastavení.',
    },
    {
        'id': 'optional',
        'label': 'Voliteľné',
        'badge': 'OPTIONAL',
        'icon': '▫️',
        'description': 'Nie je potrebné pre základnú obnovu servera. Obnov podľa potreby po tom, čo host beží.',
    },
]

RESTORE_CATEGORY_IDS = [category['id'] for category in RESTORE_CATEGORIES]

# Kategórie, pri ktorých musí používateľ pred priamym aplikovaním výslovne potvrdiť kontrolu.
REVIEW_CATEGORY_IDS = {'review', 'selective', 'reference'}

RESTORE_POLICIES = {
    'direct': 'Aplikácia môže súbor aplikovať na host (s pre-apply zálohou). Pri REVIEW/SELECTIVE/REFERENCE vyžaduje potvrdenie kontroly.',
    'stage_only': 'Aplikácia tento obsah nikdy priamo neprepíše. Môže ho iba pripraviť do review adresára v /root na ručné porovnanie.',
}

SENSITIVITY_LEVELS = {
    'normal': 'Bežná konfigurácia.',
    'sensitive': 'Obsahuje údaje o účtoch, infraštruktúre alebo prístupoch. Narábaj opatrne.',
    'secret': 'Obsahuje tajomstvá (hashované heslá, privátne kľúče, tokeny). Nikdy nezdieľaj a nezobrazuj obsah.',
}

# ---------------------------------------------------------------------------
# DR profily backup položiek (kľúč = cesta z DEFAULT_BACKUP_FILES)
# ---------------------------------------------------------------------------
# restore_order = číslo kroku v hlavnom recovery checkliste, v ktorom sa položka rieši.

RECOVERY_PROFILES = {
    '/etc/pve': {
        'restore_category': 'required',
        'restore_category_alt': None,
        'restore_order': 7,
        'sensitivity': 'secret',
        'restore_policy': 'stage_only',
        'advanced_restore': True,
        'hardware_dependent': False,
        'topics': ['storage'],
        'wiki_slug': 'pve-config-db',
        'why_backup': 'Je to srdce Proxmoxu: definície všetkých VM a LXC, storage, používatelia, oprávnenia, firewall a backup joby. Bez neho musíš celý datacenter nakonfigurovať odznova.',
        'contains': [
            'VM a LXC configy (nodes/<node>/qemu-server/*.conf, nodes/<node>/lxc/*.conf)',
            'storage.cfg – definície storage (local-lvm, ZFS, NFS, CIFS, PBS)',
            'datacenter.cfg, jobs.cfg (vzdump/replikačné joby)',
            'user.cfg, ACL, priv/shadow.cfg, priv/token.cfg, priv/tfa.cfg',
            'firewall/ (cluster.fw, <vmid>.fw), nodes/<node>/host.fw',
            'SDN, resource mappings, certifikáty nodu',
        ],
        'when_needed': 'Vždy, keď nový server má prevziať úlohu pôvodného Proxmox hosta.',
        'restore_same_hardware': 'Pri jednotlivom poškodenom súbore (napr. VM config) skopíruj iba ten súbor cez `cp` (bez -a) do bežiaceho /etc/pve. Celý adresár neprepisuj.',
        'restore_new_hardware': 'Preferuj obnovu cez config.db (kontrolovaný postup pri zastavenom pve-cluster). Alternatívne nechaj pmxcfs bežať a selektívne prenes storage.cfg, VM/LXC configy, user.cfg a firewall po kontrole.',
        'before_restore': [
            'Nový host má rovnaký hostname ako pôvodný (inak sú VM configy pod nodes/<starý-hostname>/).',
            'Storage z storage.cfg na novom HW existuje alebo ho vieš pripojiť.',
            'Názvy bridge (vmbr0, vmbr1…) z VM configov existujú v novej sieťovej konfigurácii.',
            'Firewall pravidlá nezablokujú nový management prístup.',
        ],
        'after_restore': [
            '`pvesm status` – všetky storage active',
            '`qm list` a `pct list` – vidno všetky VM/LXC',
            '`pvecm updatecerts --force` ak GUI hlási certifikačné chyby',
            'GUI: Datacenter → Permissions, Backup, Firewall',
        ],
        'warnings': [
            '/etc/pve NIE JE obyčajný adresár. Je to FUSE mount pmxcfs nad databázou config.db. `cp -r backup/etc/pve /etc/pve` nepoužívaj.',
            'pmxcfs nepodporuje chown/chmod – `cp -a` zlyhá na zachovaní vlastníka a môže nechať čiastočne prepísaný stav.',
            'Virtuálne súbory (.members, .vmlist, .version, .clusterlog, .rrd) a symlinky local/, qemu-server/, lxc/ nikdy nekopíruj.',
            'Obsahuje privátne kľúče a tokeny (priv/). Archív chráň.',
        ],
    },
    '/var/lib/pve-cluster/config.db': {
        'restore_category': 'required',
        'restore_category_alt': None,
        'restore_order': 7,
        'sensitivity': 'secret',
        'restore_policy': 'stage_only',
        'advanced_restore': True,
        'hardware_dependent': False,
        'topics': ['storage'],
        'wiki_slug': 'pve-config-db',
        'why_backup': 'SQLite databáza, z ktorej pmxcfs vytvára /etc/pve. Je to najkonzistentnejší spôsob, ako preniesť celú PVE konfiguráciu na nový host naraz.',
        'contains': [
            'Kompletný obsah /etc/pve v jednej SQLite databáze',
            'VM/LXC configy, storage, používatelia, ACL, firewall, joby, priv/ kľúče',
        ],
        'when_needed': 'Pri obnove single-node hosta na nový HW. Pre člena clustra platí iný postup (pvecm).',
        'restore_same_hardware': 'Iba ak je /etc/pve poškodený. Postup je rovnaký ako na novom HW (zastaviť pve-cluster, nahradiť, 0600, reboot).',
        'restore_new_hardware': 'Na čerstvej inštalácii, kde ešte nič nebeží: zálohuj nový config.db, zastav pve-cluster, nahraď súbor, nastav 0600, uprav /etc/hostname a /etc/hosts podľa pôvodného hosta a reštartuj.',
        'before_restore': [
            'Na novom hoste nebežia žiadne VM/LXC a nie je v clustri.',
            'Hostname a /etc/hosts budú zhodné s pôvodným hostom.',
            'Máš konzolový prístup (IPMI/monitor) pre prípad straty siete.',
            'Záloha čerstvého config.db: `cp -a /var/lib/pve-cluster/config.db /root/config.db.fresh`.',
        ],
        'after_restore': [
            '`systemctl status pve-cluster` – active, bez chýb',
            '`ls /etc/pve/nodes/` – je tam pôvodný hostname',
            '`pvesm status`, `qm list`, `pct list`',
            '`pvecm updatecerts --force` a reštart pveproxy, ak nefunguje GUI',
        ],
        'warnings': [
            'ADVANCED RESTORE: nikdy nekopíruj config.db počas behu pve-cluster – pmxcfs ho drží otvorený a prepíše/poškodí.',
            'Nekopíruj config.db na člena existujúceho clustra.',
            'Obsahuje privátne kľúče a hashované heslá PVE realmu.',
        ],
    },
    '/etc/network': {
        'restore_category': 'review',
        'restore_category_alt': None,
        'restore_order': 6,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': True,
        'topics': ['network'],
        'wiki_slug': 'network',
        'why_backup': 'Bridges, VLAN, bonding, management IP a gateway. VM configy odkazujú na názvy bridge, preto musí nová sieť zodpovedať starej logike.',
        'contains': [
            'interfaces – bridges (vmbr*), VLAN, bond, IP adresy, gateway',
            'interfaces.d/, if-up.d/ skripty',
        ],
        'when_needed': 'Pri každej obnove – ale na novom HW iba ako predloha na úpravu.',
        'restore_same_hardware': 'Na rovnakom HW môžeš interfaces obnoviť priamo a aplikovať `ifreload -a` z konzoly.',
        'restore_new_hardware': 'Najprv ručne rozbehni minimálnu management sieť, dostaň sa k backup storage, až potom porovnaj starú konfiguráciu s novými názvami NIC a prenes bridges/VLAN ručne.',
        'before_restore': [
            'Porovnaj staré a nové názvy NIC (`ip -br link` vs. snapshot ip-br-link.txt).',
            'Over MAC adresy, ak máš pinning cez .link súbory.',
            'Skontroluj, že gateway je definovaná iba raz.',
            'Maj konzolový prístup – zlá sieť ťa odstrihne.',
        ],
        'after_restore': [
            '`ip -br addr`, `ip route` – správna IP a default route',
            '`ping` gateway a NAS',
            '`bridge link` – bridge-ports sedia',
        ],
        'warnings': [
            'Nový HW má takmer vždy iné názvy NIC (napr. enp2s0 → enp1s0). Slepý prepis = server bez siete.',
            'Zmeny sa prejavia po `ifreload -a` alebo reštarte – rob to z konzoly, nie cez SSH.',
        ],
    },
    '/etc/hosts': {
        'restore_category': 'required',
        'restore_category_alt': 'review',
        'restore_order': 1,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['network'],
        'wiki_slug': 'new-hardware',
        'why_backup': 'Proxmox vyžaduje, aby hostname nodu resolvoval na jeho management IP. Pri zachovaní pôvodného hostname a IP je to presná predloha.',
        'contains': ['Mapovanie hostname ↔ management IP', 'Vlastné záznamy pre NAS, PBS a iné hosty'],
        'when_needed': 'Pri obnove config.db a pri zachovaní pôvodného hostname.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Obnov po kontrole, že IP adresa v súbore je IP, ktorú bude mať nový host.',
        'before_restore': ['Riadok `<IP> <fqdn> <hostname>` zodpovedá novej management IP.', 'Hostname je rovnaký ako v /etc/hostname.'],
        'after_restore': ['`hostname --ip-address` vracia management IP (nie 127.0.1.1)', '`systemctl restart pve-cluster` alebo reboot'],
        'warnings': ['Nesprávna IP v /etc/hosts spôsobí, že pve-cluster/pveproxy nenaštartujú korektne.'],
    },
    '/etc/hostname': {
        'restore_category': 'required',
        'restore_category_alt': 'review',
        'restore_order': 1,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['network'],
        'wiki_slug': 'new-hardware',
        'why_backup': 'Hostname určuje cestu /etc/pve/nodes/<hostname>/, kde sú configy VM a LXC. Zachovanie pôvodného hostname výrazne zjednoduší obnovu.',
        'contains': ['Krátky hostname nodu'],
        'when_needed': 'Pri každej obnove single-node hosta – ideálne zadaj pôvodný hostname už pri inštalácii.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Preferuj pôvodný hostname. Ak ho meníš, musíš presunúť VM/LXC configy do nodes/<nový-hostname>/.',
        'before_restore': ['Pôvodný host je vypnutý (dva nody s rovnakým menom v sieti robia zmätok).', '/etc/hosts obsahuje rovnaký hostname.'],
        'after_restore': ['`hostnamectl`', '`ls /etc/pve/nodes/`'],
        'warnings': ['Zmena hostname na bežiacom PVE bez presunu configov = VM „zmiznú“ z GUI.'],
    },
    '/etc/fstab': {
        'restore_category': 'review',
        'restore_category_alt': None,
        'restore_order': 6,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': True,
        'topics': ['storage'],
        'wiki_slug': 'disks-fstab',
        'why_backup': 'Popisuje lokálne aj sieťové mounty (NFS/CIFS), na ktoré môže odkazovať storage.cfg.',
        'contains': ['UUID/by-id lokálnych diskov', 'NFS/CIFS mounty', 'swap, bind mounty'],
        'when_needed': 'Keď pôvodný host mal dodatočné disky alebo sieťové mounty.',
        'restore_same_hardware': 'Obnov po kontrole, že UUID sa nezmenili (`blkid`).',
        'restore_new_hardware': 'Nikdy neprepisuj. Prenes iba sieťové mounty a lokálne mounty prepíš na nové UUID z `blkid`.',
        'before_restore': ['Porovnaj UUID v fstab s `blkid` nového hosta.', 'Sieťové mounty majú `_netdev,nofail`.', '`findmnt --verify` po úprave.'],
        'after_restore': ['`systemctl daemon-reload`', '`mount -a` bez chýb', '`findmnt`'],
        'warnings': ['Neexistujúce UUID v fstab môže pri boote zhodiť server do emergency mode.', 'Root/boot záznamy z novej inštalácie nikdy nenahrádzaj starými.'],
    },
    '/etc/resolv.conf': {
        'restore_category': 'review',
        'restore_category_alt': None,
        'restore_order': 3,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['network'],
        'wiki_slug': 'network',
        'why_backup': 'DNS servery a search doména pôvodného hosta.',
        'contains': ['nameserver', 'search/domain'],
        'when_needed': 'Pri nastavovaní management siete.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Over, že DNS servery sú z novej siete dostupné; inak nastav DNS v GUI (System → DNS).',
        'before_restore': ['DNS server je dostupný z novej management siete.'],
        'after_restore': ['`getent hosts download.proxmox.com`'],
        'warnings': [],
    },
    '/etc/passwd': {
        'restore_category': 'reference',
        'restore_category_alt': 'selective',
        'restore_order': 9,
        'sensitivity': 'sensitive',
        'restore_policy': 'stage_only',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'users-permissions',
        'why_backup': 'Zoznam lokálnych účtov a ich UID. Potrebný na rekonštrukciu vlastných účtov (napr. PAM používatelia v PVE, servisné účty) s rovnakým UID.',
        'contains': ['Systémové aj vlastné účty, UID/GID, home, shell'],
        'when_needed': 'Ak si mal vlastných lokálnych používateľov alebo PAM účty v Proxmoxe.',
        'restore_same_hardware': 'Neprepisuj celý súbor ani na rovnakom HW po reinštalácii – systémové UID sa medzi verziami líšia.',
        'restore_new_hardware': 'Iba referencia. Vlastné účty vytvor cez `useradd -u <UID>` s pôvodným UID.',
        'before_restore': ['`diff` medzi zálohou a novým /etc/passwd – nájdi iba vlastné účty (UID ≥ 1000).'],
        'after_restore': ['`getent passwd <user>`', 'Vlastníctvo súborov v /home a bind mountoch sedí'],
        'warnings': ['NIKDY neprepisuj celý /etc/passwd na novom Debian/Proxmox – rozbiješ systémové účty a vlastníctvo balíkov.'],
    },
    '/etc/group': {
        'restore_category': 'reference',
        'restore_category_alt': 'selective',
        'restore_order': 9,
        'sensitivity': 'sensitive',
        'restore_policy': 'stage_only',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'users-permissions',
        'why_backup': 'Lokálne skupiny a členstvá – referencia pre vlastné skupiny a ich GID.',
        'contains': ['Systémové aj vlastné skupiny, GID, členovia'],
        'when_needed': 'Ak si mal vlastné skupiny (napr. pre zdieľané adresáre alebo LXC bind mounty).',
        'restore_same_hardware': 'Neprepisuj celý súbor.',
        'restore_new_hardware': 'Iba referencia. Vlastné skupiny vytvor cez `groupadd -g <GID>`, členov pridaj cez `usermod -aG`.',
        'before_restore': ['`diff` zálohy a nového /etc/group – iba vlastné skupiny.'],
        'after_restore': ['`getent group <group>`'],
        'warnings': ['NIKDY neprepisuj celý /etc/group – systémové GID sa medzi inštaláciami líšia.'],
    },
    '/etc/shadow': {
        'restore_category': 'reference',
        'restore_category_alt': 'selective',
        'restore_order': 9,
        'sensitivity': 'secret',
        'restore_policy': 'stage_only',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'users-permissions',
        'why_backup': 'Hashované heslá lokálnych účtov. Umožňuje v krajnom prípade preniesť heslo konkrétneho účtu bez jeho znalosti.',
        'contains': ['Hashe hesiel, expirácia hesiel'],
        'when_needed': 'Takmer nikdy – preferuj nastavenie nových hesiel cez `passwd`.',
        'restore_same_hardware': 'Neprepisuj celý súbor.',
        'restore_new_hardware': 'Nastav heslá znova (`passwd <user>`). Ak musíš, prenes iba jeden riadok konkrétneho účtu cez `vipw -s`.',
        'before_restore': ['Je naozaj nutné preniesť hash? Nové heslo je bezpečnejšie.'],
        'after_restore': ['`passwd -S <user>`', 'Prihlásenie daného účtu funguje'],
        'warnings': [
            'SECURITY: obsahuje hashe hesiel. Aplikácia ho nikdy automaticky neprepíše a obsah nikdy nezobrazuje.',
            'Celý prepis /etc/shadow na novom systéme rozbije systémové účty a môže ťa zamknúť mimo servera.',
            'Review adresár so shadow po skončení zmaž (`rm -rf /root/proxmox-backup-restore-review-*`).',
        ],
    },
    '/etc/subuid': {
        'restore_category': 'review',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'users-permissions',
        'why_backup': 'Subordinate UID rozsahy pre unprivileged LXC. Vlastné lxc.idmap mapovania bez nich nenaštartujú.',
        'contains': ['root:100000:65536 a vlastné rozsahy'],
        'when_needed': 'Ak niektorý LXC používa vlastné `lxc.idmap` (napr. passthrough UID pre bind mount).',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Porovnaj s novým súborom; doplň iba vlastné riadky.',
        'before_restore': ['`grep idmap /etc/pve/lxc/*.conf` – ktoré mapovania potrebuješ.'],
        'after_restore': ['`pct start <vmid>` pre LXC s idmap'],
        'warnings': [],
    },
    '/etc/subgid': {
        'restore_category': 'review',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'users-permissions',
        'why_backup': 'Subordinate GID rozsahy pre unprivileged LXC. Pár k /etc/subuid.',
        'contains': ['root:100000:65536 a vlastné rozsahy'],
        'when_needed': 'Ak niektorý LXC používa vlastné `lxc.idmap`.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Porovnaj s novým súborom; doplň iba vlastné riadky.',
        'before_restore': ['`grep idmap /etc/pve/lxc/*.conf`'],
        'after_restore': ['`pct start <vmid>` pre LXC s idmap'],
        'warnings': [],
    },
    '/etc/ssh': {
        'restore_category': 'selective',
        'restore_category_alt': 'review',
        'restore_order': 9,
        'sensitivity': 'secret',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'ssh',
        'why_backup': 'sshd_config a host keys. Zachovanie host keys znamená, že klienti (aj táto aplikácia) neuvidia „REMOTE HOST IDENTIFICATION HAS CHANGED“.',
        'contains': ['sshd_config, sshd_config.d/', 'ssh_host_*_key (privátne!) a *.pub'],
        'when_needed': 'Ak chceš zachovať SSH identitu hosta alebo vlastné nastavenia sshd.',
        'restore_same_hardware': 'Obnov sshd_config a host keys, potom `systemctl restart ssh` z konzoly.',
        'restore_new_hardware': 'Selektívne: host keys iba ak je pôvodný host natrvalo mimo prevádzky; sshd_config porovnaj s verziou z novej inštalácie.',
        'before_restore': ['Pôvodný host je natrvalo vypnutý.', 'Máš konzolový prístup pre prípad chyby v sshd_config.', '`sshd -t` po úprave.'],
        'after_restore': ['`sshd -t`', '`systemctl restart ssh`', 'Prihlásenie z druhého terminálu pred zatvorením aktuálneho'],
        'warnings': [
            'Starý a nový host nesmú súčasne bežať s rovnakou SSH identitou.',
            'Privátne host keys musia mať práva 0600.',
            'Root authorized_keys na PVE je symlink do /etc/pve/priv/authorized_keys.',
        ],
    },
    '/etc/apt': {
        'restore_category': 'reference',
        'restore_category_alt': 'selective',
        'restore_order': 2,
        'sensitivity': 'normal',
        'restore_policy': 'stage_only',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'system-config',
        'why_backup': 'Prehľad, aké repozitáre (enterprise / no-subscription / vlastné) a pinning boli na pôvodnom hoste.',
        'contains': ['sources.list, sources.list.d/*.list|*.sources', 'apt.conf.d/, preferences.d/, trusted.gpg.d/, keyrings'],
        'when_needed': 'Pri nastavovaní repozitárov po čistej inštalácii.',
        'restore_same_hardware': 'Neprepisuj automaticky ani na rovnakom HW, ak sa zmenila verzia Debianu/PVE.',
        'restore_new_hardware': 'Iba referencia. Repozitáre nastav podľa verzie novej inštalácie (codename bookworm/trixie…), vlastné repozitáre pridaj ručne.',
        'before_restore': ['`cat /etc/os-release` a `pveversion` – codename musí sedieť s repozitármi.'],
        'after_restore': ['`apt update` bez chýb'],
        'warnings': ['Repozitáre so starým Debian codename na novšej inštalácii môžu spôsobiť mix verzií a rozbiť upgrade.'],
    },
    '/etc/systemd/system': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'systemd-cron',
        'why_backup': 'Vlastné services a timery (napr. backup joby) a overrides.',
        'contains': ['*.service, *.timer, *.mount', '*.wants/ symlinky, override.conf v *.d/'],
        'when_needed': 'Keď si mal vlastné služby alebo timery.',
        'restore_same_hardware': 'Obnov vlastné jednotky, potom `systemctl daemon-reload`.',
        'restore_new_hardware': 'Prenes iba vlastné jednotky; *.wants/ symlinky nevytváraj ručne, použi `systemctl enable`.',
        'before_restore': ['Zoznam vlastných jednotiek (nie z balíkov): porovnaj zálohu s novým systémom.', 'Skripty, ktoré jednotky volajú, sú už obnovené.'],
        'after_restore': ['`systemctl daemon-reload`', '`systemctl list-timers`', '`systemctl --failed`'],
        'warnings': ['Neprepisuj systémové jednotky z balíkov a symlinky v *.wants/ bez kontroly.'],
    },
    '/etc/default': {
        'restore_category': 'review',
        'restore_category_alt': None,
        'restore_order': 6,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': True,
        'topics': [],
        'wiki_slug': 'system-config',
        'why_backup': 'Parametre služieb a GRUB (napr. kernel cmdline pre IOMMU/passthrough).',
        'contains': ['grub (GRUB_CMDLINE_LINUX_DEFAULT)', 'nastavenia služieb (autofs, nfs-common, …)'],
        'when_needed': 'Ak si mal upravený GRUB (intel_iommu, amd_iommu, pcie_acs_override…) alebo služby.',
        'restore_same_hardware': 'Obnov po kontrole, potom `update-grub`.',
        'restore_new_hardware': 'Porovnaj súbor po súbore. IOMMU a HW parametre uprav podľa novej CPU platformy.',
        'before_restore': ['Intel ↔ AMD: intel_iommu vs amd_iommu.', 'Systém bootuje cez GRUB alebo systemd-boot (`proxmox-boot-tool status`)?'],
        'after_restore': ['`update-grub` alebo `proxmox-boot-tool refresh`', 'reboot a `cat /proc/cmdline`'],
        'warnings': ['Nesprávne kernel parametre môžu zabrániť bootu.'],
    },
    '/etc/modules': {
        'restore_category': 'review',
        'restore_category_alt': None,
        'restore_order': 6,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': True,
        'topics': [],
        'wiki_slug': 'system-config',
        'why_backup': 'Moduly načítané pri štarte (napr. vfio pre PCI passthrough).',
        'contains': ['vfio, vfio_iommu_type1, vfio_pci, HW-specific moduly'],
        'when_needed': 'Pri PCI/GPU passthrough alebo špeciálnom HW.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Prenes iba moduly relevantné pre nový HW.',
        'before_restore': ['`lspci -nn` nového hosta vs. snapshot lspci-nn.txt.'],
        'after_restore': ['`update-initramfs -u -k all`', '`lsmod`'],
        'warnings': ['Moduly pre HW, ktorý nový server nemá, sú zbytočné; vfio pre nesprávne zariadenie môže vziať NIC alebo GPU hostovi.'],
    },
    '/etc/modprobe.d': {
        'restore_category': 'review',
        'restore_category_alt': None,
        'restore_order': 6,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': True,
        'topics': [],
        'wiki_slug': 'system-config',
        'why_backup': 'Blacklisty a parametre modulov (vfio-pci ids, zfs_arc_max, …).',
        'contains': ['blacklist *.conf', 'options vfio-pci ids=…', 'options zfs zfs_arc_max=…'],
        'when_needed': 'Pri passthrough, ZFS tuningu alebo blacklistoch ovládačov.',
        'restore_same_hardware': 'Obnov priamo, potom `update-initramfs -u -k all`.',
        'restore_new_hardware': 'Skontroluj PCI ID (vendor:device) – na novom HW sú iné. zfs_arc_max uprav podľa RAM.',
        'before_restore': ['`lspci -nn` – sedia PCI ID?', 'Veľkosť RAM pre zfs_arc_max.'],
        'after_restore': ['`update-initramfs -u -k all`', 'reboot'],
        'warnings': ['Blacklist ovládača sieťovky/disku z iného HW môže nový server odrezať od siete alebo storage.'],
    },
    '/etc/sysctl.conf': {
        'restore_category': 'review',
        'restore_category_alt': None,
        'restore_order': 6,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'system-config',
        'why_backup': 'Vlastné kernel runtime nastavenia (forwarding, swappiness, …).',
        'contains': ['net.ipv4.ip_forward, vm.swappiness, …'],
        'when_needed': 'Ak si mal vlastné sysctl hodnoty.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Väčšinou možno obnoviť, ale najprv prejdi hodnoty.',
        'before_restore': ['Hodnoty viazané na RAM/CPU (vm.*, net.core.*) sedia s novým HW.'],
        'after_restore': ['`sysctl --system`'],
        'warnings': [],
    },
    '/etc/sysctl.d': {
        'restore_category': 'review',
        'restore_category_alt': None,
        'restore_order': 6,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'system-config',
        'why_backup': 'Doplnkové sysctl súbory.',
        'contains': ['*.conf so sysctl nastaveniami'],
        'when_needed': 'Ak si mal vlastné sysctl súbory.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Prenes iba vlastné súbory (nie tie z balíkov).',
        'before_restore': ['Ktoré súbory sú vlastné? (`dpkg -S /etc/sysctl.d/<súbor>`)'],
        'after_restore': ['`sysctl --system`'],
        'warnings': [],
    },
    '/var/spool/cron': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'sensitive',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'systemd-cron',
        'why_backup': 'Používateľské crontaby (root aj ďalší), ktoré nie sú v /etc.',
        'contains': ['crontabs/root, crontabs/<user>'],
        'when_needed': 'Ak si mal plánované úlohy cez `crontab -e`.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Obnov iba vlastné úlohy cez `crontab -e`, po obnove skriptov, ktoré volajú.',
        'before_restore': ['Skripty z cron jobov existujú (/usr/local/sbin, /root).'],
        'after_restore': ['`crontab -l`', 'Práva crontabs: 0600, skupina crontab'],
        'warnings': ['Cron joby môžu obsahovať heslá alebo tokeny v príkazoch.'],
    },
    '/etc/cron*': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'systemd-cron',
        'why_backup': 'Systémové cron súbory vrátane vlastných v /etc/cron.d.',
        'contains': ['crontab, cron.d/, cron.daily/, cron.weekly/, …'],
        'when_needed': 'Ak si mal vlastné súbory v /etc/cron.d alebo cron.daily.',
        'restore_same_hardware': 'Obnov vlastné súbory.',
        'restore_new_hardware': 'Obnov iba vlastné súbory; tie z balíkov nechaj z novej inštalácie.',
        'before_restore': ['Ktoré súbory sú vlastné? (`dpkg -S <súbor>`)'],
        'after_restore': ['`run-parts --test /etc/cron.daily`'],
        'warnings': ['Wildcard položku aplikácia neobnovuje automaticky – obnov ručne z archívu.'],
    },
    '/etc/vzdump.conf': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['storage'],
        'wiki_slug': 'systemd-cron',
        'why_backup': 'Globálne predvoľby vzdump (tmpdir, bwlimit, zstd threads, mailto…).',
        'contains': ['Globálne vzdump nastavenia'],
        'when_needed': 'Pri rekonštrukcii backup systému VM/LXC.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Obnov po kontrole ciest (tmpdir, dumpdir) a výkonu (threads).',
        'before_restore': ['Cesty v súbore existujú.'],
        'after_restore': ['Testovací vzdump jednej malej VM/LXC'],
        'warnings': [],
    },
    '/root': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'secret',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'ssh',
        'why_backup': 'Admin skripty, SSH kľúče, poznámky a dotfiles administrátora.',
        'contains': ['.ssh/ (privátne kľúče, authorized_keys, known_hosts)', 'skripty, poznámky, .bashrc'],
        'when_needed': 'Keď potrebuješ vlastné skripty alebo SSH kľúče root účtu.',
        'restore_same_hardware': 'Obnov selektívne, nie celý adresár naraz.',
        'restore_new_hardware': 'Prenes iba potrebné súbory. authorized_keys na PVE je symlink do /etc/pve/priv/ – neprepisuj symlink súborom.',
        'before_restore': ['Ktoré súbory reálne potrebuješ?', 'Pôvodné SSH kľúče nie sú kompromitované.'],
        'after_restore': ['`ls -la /root/.ssh` – práva 0700/0600', 'Symlink authorized_keys → /etc/pve/priv/authorized_keys zostal'],
        'warnings': ['SENSITIVE: obsahuje privátne kľúče a môže obsahovať heslá v skriptoch.'],
    },
    '/usr/local/bin': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'systemd-cron',
        'why_backup': 'Ručne pridané nástroje a skripty.',
        'contains': ['vlastné binárky a skripty'],
        'when_needed': 'Ak služby, timery alebo cron volajú tieto nástroje.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Obnov skripty; binárky skompilované pre iný systém radšej nainštaluj znova.',
        'before_restore': ['Binárky vs. skripty (`file <súbor>`).'],
        'after_restore': ['Práva 0755', 'Spustenie s `--help`/test'],
        'warnings': [],
    },
    '/usr/local/sbin': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['storage'],
        'wiki_slug': 'systemd-cron',
        'why_backup': 'Admin skripty vrátane vzdump orchestrátorov.',
        'contains': ['pve_vzdump_enable_run_disable.sh a ďalšie admin skripty'],
        'when_needed': 'Pri rekonštrukcii backup systému a vlastných služieb.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Obnov a skontroluj premenné (NODE, JOB_ID, IP NAS).',
        'before_restore': ['Premenné so starým hostname/IP.'],
        'after_restore': ['Práva 0755', 'Ručný test skriptu'],
        'warnings': [],
    },
    '/etc/auto.master': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['storage'],
        'wiki_slug': 'autofs-nas',
        'why_backup': 'Hlavná autofs mapa pre on-demand mounty QNAP/WD.',
        'contains': ['mount pointy a odkazy na mapy (auto.nfs)'],
        'when_needed': 'Pri obnove NAS automountov a vzdump orchestrácie.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Obnov po inštalácii `autofs nfs-common`.',
        'before_restore': ['`apt install autofs nfs-common`'],
        'after_restore': ['`systemctl restart autofs`', '`ls /autofs/<storage>`'],
        'warnings': [],
    },
    '/etc/auto.master.d': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['storage'],
        'wiki_slug': 'autofs-nas',
        'why_backup': 'Doplnkové autofs master mapy.',
        'contains': ['*.autofs súbory'],
        'when_needed': 'Ak si používal auto.master.d.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Obnov po inštalácii autofs.',
        'before_restore': ['autofs je nainštalovaný'],
        'after_restore': ['`systemctl restart autofs`'],
        'warnings': [],
    },
    '/etc/auto.nfs': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['storage'],
        'wiki_slug': 'autofs-nas',
        'why_backup': 'NFS mapy pre QNAP/WD (qnap-storage, wd-storage).',
        'contains': ['kľúč mountu, NFS options, IP:/export'],
        'when_needed': 'Pri obnove NAS automountov.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Obnov a over, že NAS povoľuje NFS prístup z IP nového hosta.',
        'before_restore': ['NAS NFS export povoľuje IP nového hosta.', '`showmount -e <NAS-IP>`'],
        'after_restore': ['`ls /autofs/<storage>`'],
        'warnings': ['Ak má nový host inú IP, NAS ho môže odmietnuť (NFS host access list).'],
    },
    '/etc/systemd/system/pve-backup-*.service': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['storage'],
        'wiki_slug': 'autofs-nas',
        'why_backup': 'Služby pre QNAP/WD vzdump orchestráciu – rekonštrukcia existujúceho backup systému.',
        'contains': ['pve-backup-*.service'],
        'when_needed': 'Nie pre prvotné nabootovanie, ale pre obnovu pravidelných záloh VM/LXC.',
        'restore_same_hardware': 'Obnov ručne z archívu.',
        'restore_new_hardware': 'Obnov ručne po obnove skriptu a autofs, potom `systemctl daemon-reload`.',
        'before_restore': ['Skript pve_vzdump_enable_run_disable.sh je obnovený.'],
        'after_restore': ['`systemctl daemon-reload`'],
        'warnings': ['Wildcard položku aplikácia neobnovuje automaticky – obnov ručne z archívu.'],
    },
    '/etc/systemd/system/pve-backup-*.timer': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['storage'],
        'wiki_slug': 'autofs-nas',
        'why_backup': 'Timery pre QNAP/WD vzdump orchestráciu.',
        'contains': ['pve-backup-*.timer'],
        'when_needed': 'Pre obnovu pravidelných záloh VM/LXC.',
        'restore_same_hardware': 'Obnov ručne z archívu.',
        'restore_new_hardware': 'Obnov ručne, `systemctl enable --now <timer>` až po teste služby.',
        'before_restore': ['Služba funguje pri ručnom spustení.'],
        'after_restore': ['`systemctl list-timers | grep pve-backup`'],
        'warnings': ['Wildcard položku aplikácia neobnovuje automaticky – obnov ručne z archívu.'],
    },
    '/usr/local/sbin/pve_vzdump_enable_run_disable.sh': {
        'restore_category': 'selective',
        'restore_category_alt': None,
        'restore_order': 9,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['storage'],
        'wiki_slug': 'autofs-nas',
        'why_backup': 'Orchestrátor: zapne storage, spustí vzdump a expiruje autofs mount.',
        'contains': ['NODE, JOB_ID, storage ID'],
        'when_needed': 'Pre obnovu pravidelných záloh VM/LXC na NAS.',
        'restore_same_hardware': 'Obnov priamo.',
        'restore_new_hardware': 'Obnov, skontroluj NODE (hostname) a JOB_ID z /etc/pve/jobs.cfg.',
        'before_restore': ['Hostname a JOB_ID sedia.'],
        'after_restore': ['`chmod 0755`', 'Ručný beh s malou VM'],
        'warnings': [],
    },
    '/opt': {
        'restore_category': 'optional',
        'restore_category_alt': 'selective',
        'restore_order': 11,
        'sensitivity': 'sensitive',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'post-recovery-checklist',
        'why_backup': 'Vlastné projekty a ručné inštalácie.',
        'contains': ['ručne inštalované aplikácie, projekty'],
        'when_needed': 'Až keď host beží a potrebuješ konkrétnu aplikáciu.',
        'restore_same_hardware': 'Obnov selektívne.',
        'restore_new_hardware': 'Obnov selektívne; aplikácie s venv/binárkami radšej nainštaluj znova.',
        'before_restore': ['Ktoré projekty reálne potrebuješ?'],
        'after_restore': ['Aplikácia štartuje'],
        'warnings': [],
    },
    '/home': {
        'restore_category': 'optional',
        'restore_category_alt': 'selective',
        'restore_order': 11,
        'sensitivity': 'sensitive',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': [],
        'wiki_slug': 'users-permissions',
        'why_backup': 'Domovské adresáre lokálnych používateľov.',
        'contains': ['používateľské dáta, SSH kľúče používateľov'],
        'when_needed': 'Ak na hoste pracujú lokálni používatelia.',
        'restore_same_hardware': 'Obnov selektívne.',
        'restore_new_hardware': 'Obnov až po vytvorení účtov s pôvodnými UID.',
        'before_restore': ['Účty existujú s rovnakými UID/GID.'],
        'after_restore': ['`ls -ln /home` – vlastníci sedia'],
        'warnings': [],
    },
    '/var/lib/vz/template': {
        'restore_category': 'optional',
        'restore_category_alt': None,
        'restore_order': 10,
        'sensitivity': 'normal',
        'restore_policy': 'direct',
        'advanced_restore': False,
        'hardware_dependent': False,
        'topics': ['storage'],
        'wiki_slug': 'vm-lxc',
        'why_backup': 'ISO obrazy a LXC šablóny.',
        'contains': ['iso/, cache/ (LXC templates)'],
        'when_needed': 'Iba ak potrebuješ konkrétnu ISO/šablónu, ktorá už nie je na stiahnutie.',
        'restore_same_hardware': 'Obnov podľa potreby.',
        'restore_new_hardware': 'Zvyčajne stiahni znova (`pveam update`, `pveam download`).',
        'before_restore': [],
        'after_restore': ['`pveam list local`'],
        'warnings': [],
    },
}

FALLBACK_RECOVERY_PROFILE = {
    'restore_category': 'review',
    'restore_category_alt': None,
    'restore_order': 9,
    'sensitivity': 'sensitive',
    'restore_policy': 'direct',
    'advanced_restore': False,
    'hardware_dependent': False,
    'topics': [],
    'wiki_slug': 'dr-overview',
    'why_backup': 'Vlastná položka bez DR klasifikácie.',
    'contains': [],
    'when_needed': 'Podľa tvojho rozhodnutia.',
    'restore_same_hardware': 'Skontroluj obsah pred obnovou.',
    'restore_new_hardware': 'Skontroluj obsah pred obnovou; nová položka nemá overenú klasifikáciu.',
    'before_restore': ['Over, čo položka obsahuje a či je viazaná na hardvér.'],
    'after_restore': [],
    'warnings': ['Neklasifikovaná vlastná položka – aplikácia ju berie ako REVIEW FIRST.'],
}

# ---------------------------------------------------------------------------
# DR metadata snapshot (backup-info/) – súbory, ktoré smie UI zobraziť
# ---------------------------------------------------------------------------
# Iba diagnostické výstupy bez tajomstiev. Konfiguračné súbory a crontab sa nezobrazujú.

HOST_SNAPSHOT_FILES = [
    ('hostname.txt', 'Hostname'),
    ('pveversion-v.txt', 'Verzia Proxmox VE (pveversion -v)'),
    ('uname-a.txt', 'Kernel (uname -a)'),
    ('lscpu.txt', 'CPU (lscpu)'),
    ('ip-br-link.txt', 'Názvy NIC a MAC (ip -br link)'),
    ('ip-br-addr.txt', 'IP adresy (ip -br addr)'),
    ('ip-addr.txt', 'IP adresy – detail (ip addr)'),
    ('ip-route.txt', 'Smerovanie (ip route)'),
    ('bridge-link.txt', 'Bridge porty (bridge link)'),
    ('lsblk-f.txt', 'Disky a filesystémy (lsblk -f)'),
    ('blkid.txt', 'UUID diskov (blkid)'),
    ('disk-by-id.txt', 'Disk ID (/dev/disk/by-id)'),
    ('findmnt.txt', 'Mounty (findmnt)'),
    ('df-h.txt', 'Obsadenie (df -h)'),
    ('pvesm-status.txt', 'Storage stav (pvesm status)'),
    ('pvs.txt', 'LVM PV (pvs)'),
    ('vgs.txt', 'LVM VG (vgs)'),
    ('lvs.txt', 'LVM LV (lvs)'),
    ('zpool-status.txt', 'ZFS pooly (zpool status)'),
    ('zfs-list.txt', 'ZFS datasety (zfs list)'),
    ('lspci-nn.txt', 'PCI zariadenia (lspci -nn)'),
    ('systemctl-failed.txt', 'Zlyhané služby (systemctl --failed)'),
    ('qm-list.txt', 'VM (qm list)'),
    ('pct-list.txt', 'LXC (pct list)'),
]

HOST_SNAPSHOT_FILE_NAMES = {name for name, _label in HOST_SNAPSHOT_FILES}

# ---------------------------------------------------------------------------
# Wiki
# ---------------------------------------------------------------------------
# Bloky: p (odsek), h (podnadpis), ul/ol (zoznam), code (príkazy), warn/danger/tip/note.
# V texte je `inline kód` podporovaný; HTML sa vždy escapuje na strane UI.


def _p(text):
    return {'type': 'p', 'text': text}


def _h(text):
    return {'type': 'h', 'text': text}


def _ul(*items):
    return {'type': 'ul', 'items': list(items)}


def _ol(*items):
    return {'type': 'ol', 'items': list(items)}


def _code(text):
    return {'type': 'code', 'text': text.strip('\n')}


def _warn(text):
    return {'type': 'warn', 'text': text}


def _danger(text):
    return {'type': 'danger', 'text': text}


def _tip(text):
    return {'type': 'tip', 'text': text}


WIKI_ARTICLES = [
    {
        'slug': 'dr-overview',
        'title': 'Disaster Recovery – prehľad',
        'summary': 'Čo táto aplikácia zálohuje, čo obnovuje automaticky a ako čítať restore kategórie.',
        'blocks': [
            _p('Scenár: pôvodný Proxmox server (napr. MSI Cubi) zomrel a potrebuješ ho rekonštruovať na novom, prípadne inom hardvéri. Táto aplikácia zálohuje KONFIGURÁCIU hosta, nie disky VM/LXC. Disky obnovuješ z vzdump/PBS/NAS záloh.'),
            _h('Dve rôzne otázky'),
            _ul(
                'Tagy critical / recommended / optional hovoria, ako dôležité je položku ZÁLOHOVAŤ.',
                'Restore kategória hovorí, ako bezpečné je položku OBNOVIŤ na nový hardvér.',
            ),
            _h('Restore kategórie'),
            _ul(
                '🟢 NEW HW REQUIRED – potrebné pre obnovu (/etc/pve, config.db, hostname, hosts).',
                '🟠 REVIEW FIRST – dôležité, ale HW-závislé; najprv porovnaj (sieť, fstab, moduly, sysctl).',
                '🔵 SELECTIVE – obnov iba vlastné položky (systemd, cron, autofs, skripty, /root).',
                '⚪ REFERENCE ONLY – nikdy neprepisuj, slúži ako predloha (passwd, group, shadow, apt).',
                '▫️ OPTIONAL – nepotrebné pre základnú obnovu (/opt, /home, ISO).',
            ),
            _h('Čo aplikácia obnovuje automaticky a čo nie'),
            _ul(
                'Nič sa neobnovuje samo. Restore vždy spúšťaš ručne, vyberáš konkrétne cesty a potvrdzuješ textom OBNOVIT.',
                'Režim „Iba pripraviť na kontrolu“ rozbalí vybrané cesty do /root/proxmox-backup-restore-review-<čas>/ bez prepisu živého systému.',
                'Režim „Aplikovať“ prepíše cieľ, ale najprv skopíruje pôvodný stav do /root/proxmox-backup-restore-preapply-<čas>/.',
                'Položky /etc/pve, config.db, passwd, group, shadow a /etc/apt aplikácia NIKDY priamo neprepíše – iba ich pripraví na kontrolu.',
                'Pri REVIEW/SELECTIVE/REFERENCE položkách musíš pred aplikovaním výslovne potvrdiť, že si ich skontroloval.',
                'Wildcard položky (pve-backup-*.service, /etc/cron*) sa obnovujú ručne z archívu.',
                'Aplikácia nerobí reload ani reštart služieb.',
            ),
            _h('Pripravenosť na obnovu'),
            _p('Stav READY / WARNING / INCOMPLETE sa počíta deterministicky iba z REQUIRED položiek: každá musí byť v zálohe, ktorá je mladšia ako limit, nebola v nej preskočená a je aj mimo hosta (na FTP). Záloha uložená iba lokálne v LXC na tom istom hoste zomrie spolu s hostom.'),
            _h('Host recovery metadata'),
            _p('Každý archív obsahuje adresár backup-info/ so snapshotom pôvodného hosta (pveversion, NIC, IP, disky, UUID, storage, PCI…). Je to REFERENCE ONLY – odpovedá na otázku „ako vyzeral server pred haváriou?“. Pozri článok DR metadata snapshot.'),
            _tip('Archív si raz za čas stiahni aj mimo NAS/FTP (napr. na notebook). Pri havárii potrebuješ archív skôr, ako budeš mať funkčnú sieť k NAS.'),
        ],
    },
    {
        'slug': 'new-hardware',
        'title': 'Obnova Proxmoxu na novom HW',
        'summary': 'Celý postup od čistej inštalácie po spustenie VM – v správnom poradí.',
        'blocks': [
            _p('Postup je navrhnutý tak, aby si v každom kroku mal funkčný systém a vedel sa vrátiť. Hlavné pravidlo: najprv sieť a prístup k zálohám, potom PVE konfigurácia, až nakoniec VM/LXC.'),
            _h('1. Čistá inštalácia'),
            _ul(
                'Nainštaluj Proxmox VE v rovnakej MAJOR verzii ako pôvodný server (pozri pveversion-v.txt v snapshote).',
                'Pri inštalácii zadaj PÔVODNÝ hostname (FQDN) a ideálne pôvodnú management IP.',
                'Filesystém (ext4/LVM vs. ZFS) zvoľ rovnaký ako pôvodný – storage.cfg počíta s local-lvm alebo local-zfs.',
            ),
            _h('2. Skontroluj nový HW'),
            _code("""
ip -br link          # názvy NIC + MAC
lsblk -f             # disky a filesystémy
ls -l /dev/disk/by-id/
lscpu | head -20     # CPU vendor/model
pveversion -v
"""),
            _p('Porovnaj s ip-br-link.txt, lsblk-f.txt, lscpu.txt a pveversion-v.txt z archívu.'),
            _h('3. Minimálna management sieť'),
            _p('Iba toľko siete, aby si sa dostal na GUI/SSH a k NAS. Starý interfaces zatiaľ neaplikuj. Pozri článok Obnova siete na inom HW.'),
            _h('4. Pripoj backup storage'),
            _code("""
apt update && apt install -y nfs-common autofs
showmount -e <NAS-IP>
mkdir -p /mnt/restore-nas
mount -t nfs <NAS-IP>:/<export> /mnt/restore-nas
"""),
            _warn('Ak NAS používa NFS host access list, povoľ na ňom IP nového hosta.'),
            _h('5. Načítaj zálohu hosta'),
            _code("""
mkdir -p /root/pve-restore-review
tar -xzf /mnt/restore-nas/proxmox_backup_*.tar.gz -C /root/pve-restore-review
chmod 700 /root/pve-restore-review
less /root/pve-restore-review/backup-info/README-RESTORE.txt
"""),
            _p('Alternatíva: nainštaluj Proxmox Backup Manager do nového LXC, vlož archív do backups/ a použi tab Obnova v režime „Iba pripraviť na kontrolu“.'),
            _danger('Archív nikdy nerozbaľuj priamo do /.'),
            _h('6. Porovnaj HW-závislú konfiguráciu'),
            _code("""
cd /root/pve-restore-review
diff -u etc/network/interfaces /etc/network/interfaces
diff -u etc/fstab /etc/fstab
diff -u etc/modules /etc/modules
diff -ru etc/modprobe.d /etc/modprobe.d
diff -ru etc/sysctl.d /etc/sysctl.d
grep -E 'hostpci|usb[0-9]' etc/pve/nodes/*/qemu-server/*.conf
"""),
            _h('7. Obnov PVE konfiguráciu'),
            _p('Postup cez config.db alebo selektívne súbory – pozri článok Obnova /etc/pve a config.db.'),
            _h('8. Skontroluj identitu a storage'),
            _ul('hostname a /etc/hosts', 'storage.cfg vs. reálne disky', 'network a bridge', 'NFS/CIFS', 'users, firewall, datacenter.cfg'),
            _h('9. Selektívne host-specific veci'),
            _p('SSH, systemd, cron, autofs, skripty, /root, /usr/local/bin a /usr/local/sbin – iba vlastné položky.'),
            _h('10. VM a LXC'),
            _p('Z existujúcich diskov alebo z vzdump/PBS záloh – pozri článok Obnova VM a LXC.'),
            _h('11. Post-recovery kontrola'),
            _p('Pozri Checklist po obnove.'),
        ],
    },
    {
        'slug': 'hw-migration',
        'title': 'Migrácia na nový HW (plánovaná)',
        'summary': 'Starý server žije a chceš prejsť na nový: presun disku alebo presun hostí po jednom, bez kolízií a s cestou späť.',
        'blocks': [
            _p('Plánovaná migrácia nie je havária: starý server beží, môžeš urobiť čerstvé zálohy tesne pred prechodom a kým nie je nový host overený, starý ostáva ako cesta späť. Postup pre úplne mŕtvy server je v článku Obnova Proxmoxu na novom HW.'),
            _h('Vyber spôsob'),
            _ul(
                'A) Presun systémového disku (NVMe/SSD) do nového stroja – jeden reboot, všetko ostane ako bolo. Rieši sa iba sieťová karta, boot, CPU a passthrough. Vhodné, ak nový HW má rovnaký typ slotu.',
                'B) Nový Proxmox vedľa starého a presun hostí po jednom cez vzdump – minúty výpadku na hosťa, najbezpečnejšie a najpredvídateľnejšie.',
                'Pokročilé: dočasný cluster + `qm migrate`/`pct migrate` (takmer bez výpadku, ale rozpustenie clustra je citlivé) alebo `qm remote-migrate` medzi samostatnými hostami (CLI preview, vyžaduje API token). Pri jednom domácom serveri ich neodporúčam.',
            ),
            _danger('Nikdy nenechaj bežať dva hosty s rovnakou IP, hostname alebo SSH identitou naraz a nikdy nespúšťaj toho istého hosťa (VM/LXC) na oboch serveroch – rovnaká MAC/IP, poškodenie dát, konflikt USB/Zigbee zariadení.'),
            _warn('Vzdump joby a vlastné backup timery smú bežať iba na jednom hoste. Ak bežia na oboch, zapisujú do toho istého dump/ adresára na NAS a prune-backups si navzájom mažú zálohy.'),
            _h('Spoločná príprava (A aj B)'),
            _ol(
                'Over Riziká obnovy v Prehľade – hostia bez vzdump zálohy musia mať zálohu pred migráciou (ručný `vzdump` funguje aj pre hostí, ktorí nie sú v žiadnom jobe).',
                'Vytvor zálohu konfigurácie hosta v appke a stiahni si offline príručku aj najnovší archív na PC.',
                'Poznač si pôvodné NIC a MAC (`ip -br link`), PCI/USB passthrough (`grep -E "hostpci|usb[0-9]" /etc/pve/nodes/*/qemu-server/*.conf`) a CPU (`lscpu`).',
                'Rovnaká alebo novšia major verzia PVE na novom HW; pri prechode Intel ↔ AMD zmeň CPU typ VM z `host` na `x86-64-v2-AES`.',
            ),
            _code("""
# čerstvá vzdump záloha všetkých hostí (alebo vyber VMID)
vzdump --all --storage <backup-storage> --mode snapshot --compress zstd
# hosť, ktorý nie je v žiadnom jobe
vzdump 122 --storage <backup-storage> --mode stop --compress zstd
"""),
            _h('Spôsob A – presun systémového disku'),
            _ol(
                'Hostí s passthrough (USB/PCI) dočasne vypni z autostartu: `qm set <id> --onboot 0`.',
                'Vypni starý server (`poweroff`), presuň disk do nového stroja a zapoj sieť.',
                'Nabootuj. Ak UEFI nevidí boot záznam, vyber disk v boot menu firmvéru a potom `proxmox-boot-tool status` / `proxmox-boot-tool refresh` (systemd-boot/ZFS) alebo `grub-install` + `update-grub` (GRUB).',
                'Na konzole oprav sieť: nový názov NIC v `bridge-ports` (pozri článok Obnova siete na inom HW), `ifreload -a`.',
                'Pri zmene CPU vendora nainštaluj microcode (`apt install intel-microcode` alebo `amd64-microcode`) a uprav IOMMU parametre v /etc/default/grub alebo /etc/kernel/cmdline.',
                'Prenastav passthrough (nové PCI adresy, USB ID), `update-initramfs -u -k all`, reboot, zapni autostart späť.',
            ),
            _code("""
ip -br link
nano /etc/network/interfaces        # bridge-ports <nový NIC>
ifreload -a && ip -br addr && ping -c3 <gateway>
lspci -nn; lsusb                    # nové ID zariadení pre passthrough
update-initramfs -u -k all
"""),
            _tip('Disk zo starého stroja je zároveň záloha: ak nový HW nefunguje, vráť disk späť do starého servera.'),
            _h('Spôsob B – nový host vedľa starého'),
            _p('1) Nainštaluj nový Proxmox s DOČASNOU IP (napr. `.3`) a dočasným alebo novým hostname. Nastav sieť/VLAN a pripoj NAS (článok Obnova siete na inom HW a Obnova AutoFS).'),
            _p('2) Prenes konfiguráciu selektívne – NIE celý config.db, kým starý host beží: definície storage zo storage.cfg, používateľov/ACL, autofs mapy, vlastné skripty. Vzdump joby a backup timery na novom hoste zatiaľ NEZAPÍNAJ. Firewall prenášaj ako posledný.'),
            _p('3) Presúvaj hostí po jednom (najprv menej dôležité, infraštruktúru ako router, DNS a správcu hesiel naplánuj na čas s možnosťou výpadku):'),
            _code("""
# na STAROM hoste
qm set <id> --onboot 0              # pct set <id> --onboot 0 pre LXC
qm shutdown <id>                   # pct shutdown <id> pre LXC
qm status <id>                     # pct status <id>: musí byť stopped
vzdump <id> --storage <backup-storage> --mode stop --compress zstd
qm status <id>                     # pct status <id>: po zálohe over stopped
# vzdump --mode stop môže pôvodne bežiacu VM znovu spustiť!
# hosťa najprv vypni; na starom ho už nespúšťaj

# na NOVOM hoste
qmrestore <dump>/vzdump-qemu-<id>-<čas>.vma.zst <id> --storage local-lvm
pct restore <id> <dump>/vzdump-lxc-<id>-<čas>.tar.zst --storage local-lvm
qm config <id> | grep -E 'net|hostpci|usb'   # bridge, VLAN tag, passthrough
qm start <id>                                 # pct start <id>
"""),
            _p('Po každom hosťovi over funkčnosť (služba, sieť, zariadenia). Až potom pokračuj ďalším.'),
            _h('Prepnutie (cutover) pri spôsobe B'),
            _ol(
                'Keď bežia všetci hostia na novom hoste, vypni starý server alebo ho odpoj zo siete.',
                'Ak má nový host prevziať pôvodnú IP: uprav /etc/network/interfaces a /etc/hosts, `ifreload -a`, `pvecm updatecerts --force`, `systemctl restart pveproxy`. Zmena hostname – pozri Nový názov nodu v článku Obnova /etc/pve a config.db.',
                'Zapni vzdump joby a backup timery na novom hoste (`systemctl enable --now <timer>`, Datacenter → Backup). Na starom musia ostať vypnuté.',
                'Proxmox Backup Manager: Nastavenia → IP hosta a root heslo (ak sa zmenili) → Test SSH → Vytvoriť zálohu teraz → skontroluj READY a Riziká obnovy.',
                'Klienti s uloženým SSH kľúčom hosta: `ssh-keygen -R <ip>`, alebo prenes pôvodné host keys (článok Obnova SSH) – iba ak starý host už nikdy nepobeží.',
            ),
            _h('Cesta späť'),
            _p('Kým starý host nevymažeš, návrat je jednoduchý: vypni hosťa na novom serveri, na starom mu zapni autostart (`qm set <id> --onboot 1`) a spusti ho. Pri spôsobe A vráť disk do pôvodného stroja. Nikdy nespúšťaj ten istý hosť na oboch naraz.'),
            _h('Vyradenie starého servera'),
            _ol(
                'Nechaj nový host bežať aspoň jeden cyklus záloh (týždeň) a over obnovu aspoň jedného hosťa z novej zálohy.',
                'Na starom: vypni autostart všetkých hostí, vzdump joby aj backup timery, potom ho vypni.',
                'Disky starého servera vymaž až keď si istý – obsahujú kópie dát a privátne kľúče (`blkdiscard` / bezpečné vymazanie).',
                'Stiahni novú offline príručku – nový HW má iné NIC, UUID a PCI ID.',
            ),
        ],
    },
    {
        'slug': 'pve-config-db',
        'title': 'Obnova /etc/pve a config.db',
        'summary': 'Prečo /etc/pve nie je obyčajný adresár a ako bezpečne obnoviť PVE konfiguráciu.',
        'blocks': [
            _h('Ako to funguje'),
            _ul(
                '/etc/pve je FUSE mount procesu pmxcfs (Proxmox Cluster File System), ktorý spúšťa služba pve-cluster.',
                'Všetky dáta /etc/pve sú uložené v SQLite databáze /var/lib/pve-cluster/config.db.',
                'Zápis do /etc/pve = zápis do config.db. Keď pve-cluster nebeží, /etc/pve je prázdny.',
                'pmxcfs nepodporuje chown/chmod, má virtuálne súbory (.members, .vmlist, .version, .clusterlog, .rrd) a symlinky (local, qemu-server, lxc → nodes/<hostname>/).',
            ),
            _danger('Nikdy nepoužívaj `cp -r backup/etc/pve /etc/pve` ani `cp -a`. Na bežiacom pmxcfs to zlyhá v polovici a na zastavenom skončí v obyčajnom adresári, ktorý pmxcfs pri štarte prekryje.'),
            _danger('Nikdy nekopíruj config.db počas behu pve-cluster – pmxcfs ho má otvorený a výsledok je poškodená databáza.'),
            _h('Varianta A: celý config.db (odporúčané pre single-node na novom HW)'),
            _p('Podmienky: čerstvá inštalácia, žiadne VM/LXC, nie je v clustri, máš konzolový prístup, hostname bude rovnaký ako pôvodný.'),
            _code("""
# 0) záloha čerstvého stavu
cp -a /var/lib/pve-cluster/config.db /root/config.db.fresh-install

# 1) zastav pmxcfs (ostatné pve* služby budú chvíľu hlásiť chyby – to je OK)
systemctl stop pve-cluster

# 2) nahraď databázu
cp /root/pve-restore-review/var/lib/pve-cluster/config.db /var/lib/pve-cluster/config.db
chown root:root /var/lib/pve-cluster/config.db
chmod 0600 /var/lib/pve-cluster/config.db

# 3) hostname a hosts podľa pôvodného hosta
cat /etc/hostname
grep -v '^#' /etc/hosts

# 4) reboot a kontrola
reboot
"""),
            _p('Po reštarte:'),
            _code("""
systemctl status pve-cluster
ls /etc/pve/nodes/
pvesm status
qm list; pct list
pvecm updatecerts --force && systemctl restart pveproxy   # ak GUI hlási certifikáty
"""),
            _tip('Ak je nainštalovaný sqlite3, pred nahradením over integritu: `sqlite3 config.db "PRAGMA integrity_check"`.'),
            _h('Varianta B: selektívne súbory (pmxcfs beží)'),
            _p('Vhodné, ak chceš novú inštaláciu ponechať a preniesť iba časť: storage, VM configy, používateľov. Kopíruj súbor po súbore cez `cp` BEZ -a.'),
            _code("""
B=/root/pve-restore-review/etc/pve
N=$(hostname)
diff -u $B/storage.cfg /etc/pve/storage.cfg
cp $B/storage.cfg /etc/pve/storage.cfg
cp $B/nodes/<starý-hostname>/qemu-server/100.conf /etc/pve/nodes/$N/qemu-server/100.conf
cp $B/nodes/<starý-hostname>/lxc/101.conf /etc/pve/nodes/$N/lxc/101.conf
cp $B/user.cfg /etc/pve/user.cfg
cp $B/jobs.cfg /etc/pve/jobs.cfg
"""),
            _warn('Firewall (firewall/cluster.fw, nodes/*/host.fw) obnov ako posledný – pravidlá so starou IP/sieťou ťa môžu odstrihnúť. Najprv skontroluj, potom zapni.'),
            _h('Nový názov nodu (iný hostname)'),
            _p('Hostname je v Proxmoxe zároveň názov nodu: configy VM/LXC sú pod /etc/pve/nodes/<hostname>/ a na názov odkazujú aj backup joby a vlastné skripty. Pri havárii je najjednoduchšie zachovať pôvodný názov. Nový názov je možný, ale treba upraviť všetky miesta nižšie.'),
            _ul(
                'Configy VM a LXC: nodes/<starý>/qemu-server/*.conf a nodes/<starý>/lxc/*.conf → presunúť na nový node.',
                'Vzdump joby v /etc/pve/jobs.cfg s riadkom `node <starý>` – inak bežia pre neexistujúci node a NEZÁLOHUJÚ NIČ (bez chyby v GUI).',
                'Vlastné systemd služby/skripty s názvom nodu (napr. `Environment=NODE=<starý>` v pve-backup-*.service).',
                'Firewall a nastavenia nodu: nodes/<starý>/host.fw a nodes/<starý>/config (ak existujú).',
                'storage.cfg s obmedzením `nodes <starý>`, DNS záznam na routeri, monitoring a záložky.',
            ),
            _warn('Poradie: configy presuň PRED obnovou VM/LXC. VMID je unikátne v celom datacentri – kým je 113.conf pod nodes/<starý>/, `pct restore 113 … --force` na novom node skončí chybou, že CT už existuje na inom node.'),
            _p('Postup po obnove config.db (nový hostname zadaný už pri inštalácii, pmxcfs beží):'),
            _code("""
OLD=nuc                     # pôvodný názov nodu
N=$(hostname -s)            # nový názov nodu
ls /etc/pve/nodes/          # vidíš starý (offline) aj nový node
mkdir -p /root/node-$OLD-backup && cp -r /etc/pve/nodes/$OLD/. /root/node-$OLD-backup/   # záloha

mv /etc/pve/nodes/$OLD/qemu-server/*.conf /etc/pve/nodes/$N/qemu-server/
mv /etc/pve/nodes/$OLD/lxc/*.conf         /etc/pve/nodes/$N/lxc/
[ -f /etc/pve/nodes/$OLD/host.fw ] && cp /etc/pve/nodes/$OLD/host.fw /etc/pve/nodes/$N/host.fw
[ -f /etc/pve/nodes/$OLD/config ]  && cp /etc/pve/nodes/$OLD/config  /etc/pve/nodes/$N/config

grep -n '^\\s*node ' /etc/pve/jobs.cfg
sed -i "s/^\\(\\s*node\\) $OLD$/\\1 $N/" /etc/pve/jobs.cfg
grep -rl "NODE=$OLD" /etc/systemd/system/ | xargs -r sed -i "s/NODE=$OLD$/NODE=$N/"
systemctl daemon-reload

qm list; pct list           # všetci hostia sú na novom node
rm -rf /etc/pve/nodes/$OLD  # až po kontrole; certifikáty pve-ssl.* nový node má vlastné
systemctl restart pveproxy
"""),
            _p('Premenovanie neskôr (už obnovený pôvodný názov): vypni všetkých hostí, zmeň /etc/hostname a riadok v /etc/hosts, reboot a urob rovnaké kroky. Detekcia v Prehľade (Riziká obnovy) upozorní, ak je vzdump job viazaný na iný node, než ako sa host volá.'),
            _h('Cluster'),
            _p('Tento postup je pre samostatný node. Pre člena clustra config.db nenahrádzaj – node vymaž z clustra (`pvecm delnode`) a nový pridaj (`pvecm add`), konfigurácia sa zosynchronizuje.'),
            _h('Čo v aplikácii'),
            _p('/etc/pve a config.db sú označené ako ADVANCED RESTORE a aplikácia ich vie iba pripraviť do review adresára. Samotné nahradenie robíš ručne podľa tohto článku.'),
        ],
    },
    {
        'slug': 'network',
        'title': 'Obnova siete na inom HW',
        'summary': 'NIC naming, bridges, VLAN, management interface, gateway a bonding.',
        'blocks': [
            _p('Najčastejšia chyba pri DR: skopírovať starý /etc/network/interfaces na nový HW. Nový HW má iné názvy NIC, takže bridge-ports odkazujú na neexistujúce rozhrania a server zostane bez siete.'),
            _h('Krok 1: zisti názvy NIC'),
            _code("""
ip -br link                       # nový host
cat backup-info/ip-br-link.txt    # pôvodný host (názov + MAC)
"""),
            _p('Príklad: starý enp2s0, enp3s0 → nový enp1s0, enp4s0. Mapuj podľa úlohy (management, VM trunk), nie podľa poradia.'),
            _h('Krok 2: minimálna management sieť'),
            _p('Cieľ je iba dostať sa na GUI/SSH a k NAS. Ostatné bridge a VLAN pridáš neskôr.'),
            _code("""
auto lo
iface lo inet loopback

iface enp1s0 inet manual

auto vmbr0
iface vmbr0 inet static
    address 192.0.2.10/24
    gateway 192.0.2.1
    bridge-ports enp1s0
    bridge-stp off
    bridge-fd 0
"""),
            _p('Ak management beží vo VLAN (napr. VLAN 10) cez VLAN-aware bridge:'),
            _code("""
auto vmbr0
iface vmbr0 inet manual
    bridge-ports enp1s0
    bridge-stp off
    bridge-fd 0
    bridge-vlan-aware yes
    bridge-vids 2-4094

auto vmbr0.10
iface vmbr0.10 inet static
    address 192.0.2.10/24
    gateway 192.0.2.1
"""),
            _code("""
ifreload -a        # z konzoly, nie cez SSH
ip -br addr; ip route
ping -c3 192.0.2.1
"""),
            _h('Krok 3: prenes zvyšok konfigurácie'),
            _ul(
                'Bridges: zachovaj NÁZVY vmbr* – VM configy na ne odkazujú (net0: …,bridge=vmbr1).',
                'VLAN: bridge-vlan-aware a bridge-vids prenes; VLAN tagy sú vo VM configoch.',
                'Bonding: bond-slaves prepíš na nové NIC, bond-mode musí sedieť so switchom (LACP 802.3ad).',
                'Gateway: iba raz v celom súbore.',
                'MTU: ak si mal jumbo frames pre NAS, nastav ich aj na novom NIC aj bridge.',
            ),
            _h('Stabilné názvy NIC'),
            _p('Ak chceš zachovať pôvodné názvy, vytvor systemd .link súbor viazaný na MAC (napr. /etc/systemd/network/10-mgmt.link s [Match] MACAddress=… a [Link] Name=…), potom `update-initramfs -u` a reboot. Novšie verzie PVE majú na to aj vlastný nástroj – pozri dokumentáciu tvojej verzie.'),
            _warn('Zmeny siete vždy aplikuj s konzolovým prístupom (monitor/klávesnica, IPMI, KVM).'),
        ],
    },
    {
        'slug': 'disks-fstab',
        'title': 'Obnova diskov a /etc/fstab',
        'summary': 'UUID, /dev/disk/by-id, lokálne SSD/NVMe, NFS/CIFS a rozdiel medzi nimi.',
        'blocks': [
            _danger('Starý /etc/fstab na nový HW nikdy neprepisuj. Neexistujúce UUID môže pri boote zhodiť server do emergency mode.'),
            _h('Lokálne disky'),
            _ul(
                'UUID filesystému sa mení pri každom novom mkfs. Nový disk = nové UUID.',
                '/dev/disk/by-id/ obsahuje model a sériové číslo disku – na inom disku je iné.',
                '/dev/sdX a /dev/nvmeXnY sa môžu medzi bootmi meniť – nepoužívaj ich v fstab.',
                'Root a boot záznamy nechaj z novej inštalácie.',
            ),
            _code("""
blkid                                   # nový host
cat backup-info/blkid.txt               # pôvodný host
cat backup-info/lsblk-f.txt
"""),
            _p('Ak pôvodný dátový disk prežil a presunul si ho do nového servera, jeho UUID zostáva rovnaké – záznam môžeš prevziať. Ak je disk nový, vytvor filesystém a do fstab daj nové UUID.'),
            _h('Sieťové mounty (NFS/CIFS)'),
            _p('Sieťové mounty nie sú viazané na HW – dajú sa prevziať, ak sedí IP NAS a export. Použi bezpečné voľby:'),
            _code("""
<NAS-IP>:/export  /mnt/nas  nfs   defaults,_netdev,nofail,x-systemd.device-timeout=10s  0 0
//<NAS-IP>/share  /mnt/smb  cifs  credentials=/root/.smbcred,_netdev,nofail  0 0
"""),
            _tip('Pre NAS zálohy je autofs (on-demand) spoľahlivejší ako fstab – pozri článok AutoFS / QNAP / WD.'),
            _h('ZFS a LVM'),
            _code("""
zpool import              # zoznam poolov na pripojených diskoch
zpool import -f <pool>    # import poolu z pôvodného servera
vgscan && vgchange -ay    # LVM
pvesm status
"""),
            _p('Storage ID v /etc/pve/storage.cfg musia sedieť s reálnymi poolmi/VG. Ak sa pool volá inak, uprav storage.cfg, nie VM configy.'),
            _h('Overenie'),
            _code("""
findmnt --verify
systemctl daemon-reload
mount -a
findmnt
"""),
        ],
    },
    {
        'slug': 'users-permissions',
        'title': 'Obnova používateľov a permissions',
        'summary': 'passwd, group, shadow, subuid/subgid a PVE používatelia – prečo nikdy celý súbor.',
        'blocks': [
            _danger('/etc/passwd, /etc/group a /etc/shadow nikdy neprepisuj celé. Systémové účty (systemd-*, _apt, messagebus…) majú na novej inštalácii iné UID/GID a prepis rozbije vlastníctvo súborov a služby.'),
            _h('Linux účty'),
            _code("""
B=/root/pve-restore-review
awk -F: '$3>=1000 && $3<65534' $B/etc/passwd     # vlastné účty
awk -F: '$3>=1000 && $3<65534' $B/etc/group      # vlastné skupiny

groupadd -g <GID> <skupina>
useradd -m -u <UID> -g <GID> -s /bin/bash <user>
passwd <user>              # NOVÉ heslo – preferované
"""),
            _p('Ak naozaj musíš zachovať pôvodné heslo, prenes iba riadok daného účtu z shadow cez `vipw -s` (a `vigr -s` pre gshadow).'),
            _warn('SECURITY: /etc/shadow obsahuje hashe hesiel. Review adresár po dokončení zmaž: `rm -rf /root/proxmox-backup-restore-review-*`.'),
            _h('Proxmox používatelia'),
            _ul(
                'PVE realm (pve): používatelia a heslá sú v /etc/pve (user.cfg, priv/shadow.cfg) – prídu s config.db.',
                'PAM realm (pam): v /etc/pve je iba záznam a oprávnenia; Linux účet musíš vytvoriť ručne s rovnakým menom.',
                'API tokeny (priv/token.cfg) a 2FA (priv/tfa.cfg) prídu s config.db.',
            ),
            _h('subuid / subgid a unprivileged LXC'),
            _p('Default je root:100000:65536. Ak niektorý LXC používa vlastné `lxc.idmap`, musia zodpovedajúce rozsahy existovať v /etc/subuid a /etc/subgid, inak kontajner nenaštartuje.'),
            _code("""
grep -H idmap /etc/pve/lxc/*.conf
diff -u $B/etc/subuid /etc/subuid
diff -u $B/etc/subgid /etc/subgid
"""),
            _h('Permissions dát'),
            _p('Unprivileged LXC vidí UID posunuté o 100000. Ak obnovuješ bind-mount dáta, over vlastníka cez `ls -ln` – musí sedieť s mapovaním.'),
        ],
    },
    {
        'slug': 'ssh',
        'title': 'Obnova SSH',
        'summary': 'sshd_config, host keys, authorized_keys a zachovanie SSH identity hosta.',
        'blocks': [
            _h('Čo je v /etc/ssh'),
            _ul(
                'sshd_config a sshd_config.d/ – nastavenia servera.',
                'ssh_host_*_key – PRIVÁTNE host kľúče (identita servera), ssh_host_*_key.pub – verejné.',
            ),
            _h('Zachovať identitu hosta?'),
            _p('Ak obnovíš pôvodné host keys, klienti (aj táto aplikácia v LXC) sa pripoja bez varovania „REMOTE HOST IDENTIFICATION HAS CHANGED“. Ak nie, na klientoch spusti `ssh-keygen -R <host>`.'),
            _danger('Starý a nový host nesmú bežať súčasne s rovnakou SSH identitou. Host keys obnovuj iba vtedy, keď je pôvodný server natrvalo mimo prevádzky.'),
            _code("""
B=/root/pve-restore-review
cp $B/etc/ssh/ssh_host_* /etc/ssh/
chmod 600 /etc/ssh/ssh_host_*_key
chmod 644 /etc/ssh/ssh_host_*_key.pub
diff -u $B/etc/ssh/sshd_config /etc/ssh/sshd_config
sshd -t && systemctl restart ssh
"""),
            _warn('Pred zatvorením aktuálnej SSH session over prihlásenie z druhého terminálu.'),
            _h('authorized_keys'),
            _p('Na Proxmoxe je /root/.ssh/authorized_keys symlink na /etc/pve/priv/authorized_keys. Po obnove config.db sú kľúče späť automaticky. Symlink neprepisuj obyčajným súborom.'),
            _h('/root/.ssh'),
            _p('Privátne kľúče roota (id_*) obnov iba ak ich potrebuješ (napr. prístup na NAS/PBS). Práva: adresár 0700, kľúče 0600.'),
        ],
    },
    {
        'slug': 'systemd-cron',
        'title': 'Obnova vlastných systemd služieb a cronov',
        'summary': 'Ako nájsť vlastné služby, timery a cron joby a obnoviť iba tie.',
        'blocks': [
            _h('Nájdi vlastné jednotky'),
            _code("""
B=/root/pve-restore-review
ls -la $B/etc/systemd/system/
for f in $B/etc/systemd/system/*.service $B/etc/systemd/system/*.timer; do
  n=$(basename "$f"); dpkg -S "/etc/systemd/system/$n" >/dev/null 2>&1 || echo "vlastná: $n"
done
cat $B/backup-info/systemctl-timers.txt
"""),
            _h('Obnov'),
            _ol(
                'Najprv obnov skripty, ktoré jednotky volajú (/usr/local/sbin, /usr/local/bin) a nastav 0755.',
                'Skopíruj iba vlastné .service/.timer súbory do /etc/systemd/system/.',
                '`systemctl daemon-reload`',
                'Službu otestuj ručne (`systemctl start x.service`, `journalctl -u x`).',
                'Až potom `systemctl enable --now x.timer`.',
            ),
            _warn('Adresáre *.wants/ nekopíruj – symlinky vytvorí `systemctl enable`. Systémové jednotky z balíkov neprepisuj.'),
            _h('Cron'),
            _code("""
cat $B/backup-info/crontab-root.txt
crontab -e                       # prenes iba vlastné riadky
ls $B/etc/cron.d/
"""),
            _p('Súbory v /etc/cron.d, ktoré nepatria balíku (`dpkg -S`), skopíruj s právami 0644. Crontaby v /var/spool/cron/crontabs musia mať 0600 a skupinu crontab – preto radšej `crontab -e`.'),
            _h('vzdump.conf'),
            _p('Globálne predvoľby vzdump. Obnov po kontrole ciest (tmpdir) a počtu vlákien podľa nového CPU.'),
        ],
    },
    {
        'slug': 'autofs-nas',
        'title': 'Obnova AutoFS / QNAP / WD mountov',
        'summary': 'On-demand NFS mounty pre NAS zálohy a vzdump orchestrácia.',
        'blocks': [
            _h('Predpoklady'),
            _code("""
apt update && apt install -y autofs nfs-common
showmount -e <QNAP-IP>
showmount -e <WD-IP>
"""),
            _warn('Ak má nový host inú IP, povoľ ju na NAS v NFS prístupových právach (QNAP: Shared Folders → NFS host access). Inak mount skončí „access denied“.'),
            _h('Obnova máp'),
            _code("""
B=/root/pve-restore-review
cp $B/etc/auto.master /etc/auto.master
cp -r $B/etc/auto.master.d/. /etc/auto.master.d/ 2>/dev/null || true
cp $B/etc/auto.nfs /etc/auto.nfs
systemctl enable --now autofs
systemctl restart autofs
ls -la /autofs/<storage>
"""),
            _h('Vzdump orchestrácia'),
            _code("""
cp $B/usr/local/sbin/pve_vzdump_enable_run_disable.sh /usr/local/sbin/
chmod 0755 /usr/local/sbin/pve_vzdump_enable_run_disable.sh
cp $B/etc/systemd/system/pve-backup-*.service /etc/systemd/system/
cp $B/etc/systemd/system/pve-backup-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl list-timers | grep pve-backup
"""),
            _ul(
                'V skripte skontroluj NODE (hostname), JOB_ID (/etc/pve/jobs.cfg) a storage ID.',
                'PVE storage pre NAS (storage.cfg) by mal mať `is_mountpoint` nastavené, aby vzdump nepísal na lokálny disk, keď mount chýba.',
                'Službu najprv spusti ručne s malou VM, až potom zapni timer.',
            ),
        ],
    },
    {
        'slug': 'vm-lxc',
        'title': 'Obnova VM a LXC',
        'summary': 'Configy sú v /etc/pve, disky sú inde. Ako ich dať opäť dokopy.',
        'blocks': [
            _p('Táto aplikácia zálohuje iba DEFINÍCIE VM/LXC (v /etc/pve). Disky obnovíš jedným z troch spôsobov.'),
            _h('A) Disky prežili (presunutý NVMe/SSD)'),
            _code("""
zpool import -f <pool>      # alebo vgchange -ay pre LVM
pvesm status
qm config 100 | grep -E 'scsi|virtio|sata|ide'
"""),
            _p('Ak storage.cfg a configy sedia, VM sa objavia s pôvodnými diskami.'),
            _h('B) Restore z vzdump (NAS)'),
            _code("""
ls /autofs/<storage>/dump/
qmrestore /autofs/<storage>/dump/vzdump-qemu-100-<čas>.vma.zst 100 --storage local-lvm
pct restore 101 /autofs/<storage>/dump/vzdump-lxc-101-<čas>.tar.zst --storage local-lvm
"""),
            _warn('Ak už config s rovnakým VMID existuje (z config.db), qmrestore/pct restore odmietne prepísať. Rozhodni, či použiješ --force (prepíše config zo zálohy) alebo iné VMID.'),
            _h('C) Proxmox Backup Server'),
            _p('Pridaj PBS storage (Datacenter → Storage → Add → Proxmox Backup Server, fingerprint z PBS), potom restore cez GUI alebo `qmrestore <pbs-storage>:backup/vm/100/<čas> 100`.'),
            _h('Pred prvým štartom skontroluj'),
            _ul(
                'Bridge (net0: …,bridge=vmbrX) existuje.',
                'CPU typ: `host` pri prechode Intel ↔ AMD môže zlyhať – použi x86-64-v2-AES alebo podobný.',
                'Passthrough (hostpci, usb) – PCI adresy a USB ID sú na novom HW iné; dočasne ich odober.',
                'Resource mappings (Datacenter → Resource Mappings) prepoj na nový HW.',
                'Autostart: počas obnovy vypni `qm set <id> --onboot 0`, zapni po overení.',
            ),
            _h('ISO a šablóny'),
            _code("""
pveam update
pveam available | grep debian
pveam download local <template>
"""),
        ],
    },
    {
        'slug': 'backup-manager-recovery',
        'title': 'Obnova Proxmox Backup Managera (LXC)',
        'summary': 'Ako dostať späť samotnú appku: z vzdump zálohy LXC alebo novou inštaláciou.',
        'blocks': [
            _p('Appka (táto stránka) beží v samostatnom LXC na Proxmox hoste. Pri havárii hosta zomrie spolu s ním – preto si stiahni offline príručku (Prehľad → Stiahnuť offline príručku) a maj ju mimo servera. Na obnovu hosta appku nepotrebuješ, ale uľahčí ti ju.'),
            _h('A) Obnova LXC z vzdump (odporúčané)'),
            _p('Získaš presný pôvodný stav: prod aj dev inštanciu, históriu záloh, nastavenia FTP/SSH, admin účet a 2FA.'),
            _code("""
ls -lt /mnt/<nas>/dump/vzdump-lxc-<VMID>-*.tar.zst | head -3
pct restore <VMID> /mnt/<nas>/dump/vzdump-lxc-<VMID>-<čas>.tar.zst --storage local-lvm --force
pct config <VMID> | grep net0      # bridge, tag (VLAN), ip, gw
pct start <VMID>
pct exec <VMID> -- systemctl is-active proxmox-backup.service
"""),
            _p('`--force` je potrebné, ak config LXC už prišiel s obnoveným config.db. Potom v appke: Nastavenia → Test SSH. Nový SSH kľúč hosta appke nevadí; ak má nový host iné root heslo, zadaj ho do Nastavení.'),
            _h('B) Nová inštalácia (vzdump LXC chýba)'),
            _code("""
pveam update
pveam available --section system | grep debian-12
pveam download local debian-12-standard_<verzia>_amd64.tar.zst

pct create <VMID> local:vztmpl/debian-12-standard_<verzia>_amd64.tar.zst \\
  --hostname proxmox-backup --cores 2 --memory 2048 --swap 512 --rootfs local-lvm:20 \\
  --net0 name=eth0,bridge=vmbr0,tag=<VLAN>,ip=<IP>/24,gw=<GW> \\
  --nameserver <DNS> --unprivileged 1 --features nesting=1 --onboot 1 --password
pct start <VMID> && pct enter <VMID>

apt update && apt install -y curl git
bash -c "$(curl -fsSL https://raw.githubusercontent.com/spekulanter/proxmox-backup/main/install_in_lxc.sh)"
"""),
            _p('Inštalátor vytvorí /opt/proxmox-backup, službu proxmox-backup.service (port 5000) a timer automatickej zálohy. Voliteľná dev inštancia (port 5001):'),
            _code("""
git clone -b dev https://github.com/spekulanter/proxmox-backup.git /opt/proxmox-backup-dev
cd /opt/proxmox-backup-dev && python3 -m venv venv
chmod +x update.sh auto_backup.sh test.sh && ./update.sh
"""),
            _h('Čo zadať po novej inštalácii'),
            _ol(
                'Registrácia admina a nové 2FA – recovery kódy si ulož mimo servera.',
                'Nastavenia → Zdroj: Remote SSH, IP Proxmox hosta, port 22, root + heslo → Test SSH.',
                'Nastavenia → FTP: host, port, používateľ, heslo a cieľový adresár (hodnoty sú v offline príručke) → Test pripojenia.',
                'Retencia a automatická záloha (frekvencia, deň, čas).',
                'Účet → Pushover, ak ho používaš.',
                'História: archívy z FTP sa zobrazia automaticky, pri použití sa stiahnu do lokálneho cache.',
            ),
            _h('Stratené 2FA alebo heslo'),
            _p('Použi recovery kód (Obnoviť heslo alebo 2FA). Krajná možnosť priamo v LXC: `systemctl stop proxmox-backup`, presuň /opt/proxmox-backup/auth_config.json mimo, `systemctl start proxmox-backup` – appka ponúkne novú registráciu admina. Nastavenia FTP/SSH a história zostanú.'),
            _tip('Po každej väčšej zmene prostredia (IP, VLAN, NAS, VMID) si stiahni novú offline príručku – generuje sa z najnovšieho archívu.'),
        ],
    },
    {
        'slug': 'system-config',
        'title': 'APT, kernel moduly, modprobe, sysctl a /etc/default',
        'summary': 'HW- a verziovo-závislé systémové nastavenia, ktoré treba porovnať.',
        'blocks': [
            _h('APT repozitáre (REFERENCE ONLY)'),
            _code("""
B=/root/pve-restore-review
cat /etc/os-release | grep CODENAME
grep -rh '^deb\\|^URIs\\|^Suites' $B/etc/apt/sources.list $B/etc/apt/sources.list.d/
"""),
            _ul(
                'Codename (bookworm, trixie…) musí sedieť s novou inštaláciou.',
                'Enterprise repo vyžaduje subskripciu – bez nej použi no-subscription.',
                'Vlastné repozitáre pridaj ručne aj s ich kľúčom (signed-by).',
            ),
            _h('Kernel moduly a modprobe (REVIEW FIRST)'),
            _code("""
diff -u $B/etc/modules /etc/modules
diff -ru $B/etc/modprobe.d /etc/modprobe.d
lspci -nn                         # nové PCI ID
cat $B/backup-info/lspci-nn.txt   # pôvodné PCI ID
update-initramfs -u -k all
"""),
            _warn('`options vfio-pci ids=…` a blacklisty z pôvodného HW môžu na novom HW zobrať hostovi sieťovku alebo GPU.'),
            _h('sysctl (REVIEW FIRST)'),
            _code("""
diff -u $B/etc/sysctl.conf /etc/sysctl.conf
diff -ru $B/etc/sysctl.d /etc/sysctl.d
sysctl --system
"""),
            _h('/etc/default a GRUB (REVIEW FIRST)'),
            _code("""
diff -u $B/etc/default/grub /etc/default/grub
proxmox-boot-tool status        # GRUB vs systemd-boot
update-grub                     # alebo: proxmox-boot-tool refresh
"""),
            _p('Pri prechode Intel ↔ AMD zmeň intel_iommu=on na amd_iommu=on (pri novších kerneloch je IOMMU často zapnuté automaticky).'),
        ],
    },
    {
        'slug': 'host-snapshot',
        'title': 'DR metadata snapshot',
        'summary': 'Ako vyzeral pôvodný server pred haváriou – NIC, IP, disky, UUID, storage, PCI.',
        'blocks': [
            _p('Pri každej zálohe sa na hoste spustia diagnostické príkazy a ich výstup sa uloží do backup-info/ v archíve. Ak príkaz na hoste neexistuje (napr. zpool bez ZFS), zapíše sa iba chyba – záloha kvôli tomu nezlyhá.'),
            _ul(
                'pveversion -v, hostname, uname -a, lscpu',
                'ip -br link, ip -br addr, ip addr, ip route, bridge link',
                'lsblk -f, blkid, /dev/disk/by-id, findmnt, df -h',
                'pvesm status, pvs, vgs, lvs, zpool status, zfs list',
                'lspci -nn, systemctl --failed, qm list, pct list',
            ),
            _p('Tieto dáta sú REFERENCE ONLY. Nikdy sa automaticky neobnovujú. Na karte Obnova na novom HW → Snapshot hosta ich vieš zobraziť pre ľubovoľný archív.'),
            _p('Ďalej backup-info/ obsahuje recovery-manifest.json (klasifikácia zálohovaných položiek), README-RESTORE.txt a ďalšie výstupy (pvesm config, backup joby, crontab, zoznam balíkov), ktoré UI z bezpečnostných dôvodov nezobrazuje – nájdeš ich v rozbalenom archíve.'),
        ],
    },
    {
        'slug': 'post-recovery-checklist',
        'title': 'Checklist po obnove',
        'summary': 'Príkazy na overenie, že obnovený host je naozaj v poriadku.',
        'blocks': [
            _code("""
pveversion
pvesm status
ip -br addr
ip route
findmnt
mount | grep -E 'nfs|cifs'
systemctl --failed
qm list
pct list
cat /etc/pve/jobs.cfg
pve-firewall status
getent hosts download.proxmox.com
timedatectl; chronyc tracking
"""),
            _ul(
                'Storage: všetky „active“, NAS dostupný, vzdump má kam písať.',
                'VM/LXC: štartujú, majú sieť, správny bridge a VLAN.',
                'Backup joby: Datacenter → Backup; spusti „Run now“ pre jednu malú VM.',
                'Firewall: zapnutý a GUI/SSH stále dostupné.',
                'DNS a čas/NTP: správne (TLS, PBS a 2FA citlivé na čas).',
                'Proxmox Backup Manager: Test SSH, nová záloha hosta, stav pripravenosti READY.',
                'Upratanie: zmaž review adresáre a dočasné rozbalené archívy v /root.',
            ),
            _tip('Po úspešnej obnove hneď vytvor novú zálohu hosta – nový HW má nové UUID, NIC a PCI ID a snapshot musí zodpovedať realite.'),
        ],
    },
]

WIKI_ARTICLE_SLUGS = [article['slug'] for article in WIKI_ARTICLES]

# Plánovaná migrácia: príkazy sú návody pre administrátora, appka ich nespúšťa.
MIGRATION_METHODS = [
    {'id': 'disk_move', 'title': 'Presun systémového disku',
     'description': 'Starý host vypneš a jeho disk presunieš do nového stroja.'},
    {'id': 'side_by_side', 'title': 'Nový host vedľa starého',
     'description': 'Samostatný nový Proxmox s dočasnou IP a hostname; hostí presúvaš po jednom cez vzdump.'},
]
MIGRATION_COMPARE_COMMANDS = [
    {'id': 'hostname', 'title': 'Názov hosta', 'command': 'LC_ALL=C hostname'},
    {'id': 'version', 'title': 'Verzia PVE', 'command': 'LC_ALL=C pveversion -v'},
    {'id': 'storage', 'title': 'Storage', 'command': 'LC_ALL=C pvesm status'},
    {'id': 'links', 'title': 'Sieťové rozhrania', 'command': 'LC_ALL=C ip -br link'},
    {'id': 'addresses', 'title': 'IP adresy', 'command': 'LC_ALL=C ip -br addr'},
    {'id': 'network', 'title': 'Bridge a VLAN', 'command': 'LC_ALL=C cat /etc/network/interfaces'},
    {'id': 'cpu', 'title': 'CPU', 'command': 'LC_ALL=C lscpu'},
    {'id': 'qm', 'title': 'Virtuálne stroje', 'command': 'LC_ALL=C qm list'},
    {'id': 'pct', 'title': 'Kontajnery', 'command': 'LC_ALL=C pct list'},
    {'id': 'timers', 'title': 'Systemd timery',
     'command': 'LC_ALL=C systemctl list-timers --all --no-pager --no-legend'},
]

MIGRATION_GUEST_TRANSITIONS = {
    'pending': ['stopped_on_old', 'skipped'],
    'stopped_on_old': ['restored_on_new', 'skipped'],
    'restored_on_new': ['verified'],
    'verified': [],
    'skipped': ['pending'],
}
MIGRATION_STEPS = [
    {
        'id': 'prepare', 'methods': ['disk_move', 'side_by_side'],
        'title': 'Príprava a kontroly', 'goal': 'Poznať závislosti, nový HW a plán výpadku.',
        'tasks': ['Over verziu PVE, NIC/MAC, bridge/VLAN, CPU vendor a PCI/USB passthrough.',
                  'Naplánuj presun routera, DNS a LXC s touto appkou; maj konzolu a offline príručku.',
                  'Priprav samostatnú zálohu bind mountov a diskov vylúčených z vzdump.'],
        'commands': ['pveversion -v', 'ip -br link', 'lscpu', 'lspci -nn', 'qm list', 'pct list'],
        'warnings': ['Pri Intel ↔ AMD uprav VM CPU typu host podľa kompatibility.',
                     'Nikdy dva hosty s rovnakou IP, hostname alebo SSH identitou naraz.'],
        'wiki_slug': 'hw-migration',
    },
    {
        'id': 'fresh-backups', 'methods': ['disk_move', 'side_by_side'],
        'title': 'Čerstvé zálohy', 'goal': 'Mať aktuálnu konfiguráciu aj disky hostí mimo starého servera.',
        'tasks': ['Skontroluj Riziká obnovy. Hosť bez vzdump jobu potrebuje ručnú zálohu.',
                  'Vytvor zálohu hosta v appke, stiahni archív aj offline príručku na PC.',
                  'Over úspešné vzdump úlohy a dostupnosť záloh na NAS; job nie je dôkaz úspešnej zálohy.'],
        'commands': ['# STARÝ HOST – nahraď BACKUP_STORAGE reálnym ID',
                     'vzdump --all --storage BACKUP_STORAGE --mode snapshot --compress zstd'],
        'warnings': ['Nahraď BACKUP_STORAGE reálnym ID. Konfiguračný archív hosta neobsahuje disky VM/LXC.',
                     'Backup joby a timery smú zapisovať a prune-backups mazať zálohy iba z jedného hosta.'],
        'wiki_slug': 'vm-lxc',
    },
    {
        'id': 'new-host', 'methods': ['side_by_side'],
        'title': 'Nový host s dočasnou identitou', 'goal': 'Pripraviť nový samostatný Proxmox bez kolízie so starým.',
        'tasks': ['Nainštaluj rovnakú alebo novšiu podporovanú verziu PVE.',
                  'Použi inú dočasnú IP aj hostname a vlastné SSH host keys.',
                  'Z konzoly nastav NIC, bridge/VLAN a NAS; povoľ dočasnú IP na NAS.'],
        'commands': ['hostname', 'ip -br link', 'ip -br addr', 'pvesm status'],
        'warnings': ['Neklonuj SSH identitu bežiaceho starého servera.', 'Backup joby a timery na novom zatiaľ nezapínaj.'],
        'wiki_slug': 'network',
    },
    {
        'id': 'selective-config', 'methods': ['side_by_side'],
        'title': 'Selektívny prenos konfigurácie', 'goal': 'Preniesť iba skontrolované nastavenia vhodné pre nový host.',
        'tasks': ['Po kontrole prenes storage ID, používateľov/ACL, autofs mapy a vlastné skripty.',
                  'Sieť, fstab, passthrough a firewall prispôsob novému HW; firewall prenášaj posledný.',
                  'Vzdump joby a timery ponechaj na novom vypnuté.'],
        'commands': ['pvesm status', 'systemctl list-timers', 'systemctl --failed'],
        'warnings': ['NIE celý config.db, kým starý host beží. Nekopíruj celý /etc/pve ani SSH host keys.',
                     'Restore ochrany appky platia aj počas migrácie.'],
        'wiki_slug': 'hw-migration',
    },
    {
        'id': 'move-guests', 'methods': ['side_by_side'],
        'title': 'Presun hostí po jednom', 'goal': 'Vypnúť pôvodného hosťa, zálohovať, obnoviť a overiť novú kópiu.',
        'tasks': ['Použi zoznam hostí nižšie: vypni autostart, vypni hosťa a over stopped pred aj po vzdump.',
                  'Na novom vyber správny archív a storage, obnov bez --force, skontroluj sieť/passthrough.',
                  'Pred štartom na novom znovu over vypnutie na starom; over služby a až potom označ verified.'],
        'commands': [],
        'warnings': ['Nikdy ten istý hosť bežiaci na oboch serveroch.',
                     'vzdump --mode stop môže pôvodne bežiacu VM znovu spustiť; najprv ju samostatne vypni.',
                     'Skipped znamená vedomé vynechanie. Poznač dôvod a čo bude so službou po vypnutí starého hosta.'],
        'wiki_slug': 'vm-lxc',
    },
    {
        'id': 'disk-move', 'methods': ['disk_move'],
        'title': 'Vypnutie a presun disku', 'goal': 'Spustiť pôvodnú inštaláciu na novom HW.',
        'tasks': ['Vypni autostart hostí s passthrough, hostí aj starý server; až potom presuň disk.',
                  'Over kompatibilitu bootovania UEFI/GRUB a dostupnosť všetkých diskov.',
                  'Z konzoly oprav bridge-ports, CPU/microcode a PCI/USB; autostart zapni až po kontrole.'],
        'commands': ['ip -br link', 'proxmox-boot-tool status', 'ip -br addr', 'lspci -nn', 'lsusb'],
        'warnings': ['Starý stroj nesmie súčasne bootovať kópiu tej istej inštalácie.',
                     'Pri probléme nový vypni a vráť disk do pôvodného servera.'],
        'wiki_slug': 'hw-migration',
    },
    {
        'id': 'cutover', 'methods': ['disk_move', 'side_by_side'],
        'title': 'Prepnutie (cutover)', 'goal': 'Prepnúť prevádzku aj správu záloh na nový host.',
        'tasks': ['Pri side_by_side musia byť všetci hostia verified alebo vedome skipped.',
                  'Vypni alebo odpoj starý server skôr, než nový prevezme pôvodnú IP/hostname/SSH identitu.',
                  'V Nastaveniach appky zmeň SSH hosta a prípadné heslo, ulož a použi Test SSH.',
                  'Vzdump joby a backup timery aktivuj iba na novom; na starom ostanú vypnuté.'],
        'commands': ['hostname', 'ip -br addr', 'systemctl list-timers'],
        'warnings': ['Stavy sú ručné potvrdenia administrátora, nie živá kontrola hostov.',
                     'Zmena názvu existujúceho PVE nodu potrebuje osobitný postup podľa wiki.'],
        'wiki_slug': 'hw-migration',
    },
    {
        'id': 'verify-rollback', 'methods': ['disk_move', 'side_by_side'],
        'title': 'Overenie a cesta späť', 'goal': 'Overiť služby, novú zálohu a použiteľný návrat.',
        'tasks': ['Over sieť, NAS, služby hostí, firewall, čas a úspešnú novú zálohu hosta aj vzdump.',
                  'Pri návrate najprv vypni novú kópiu hosťa, až potom spusti starú.',
                  'Zmeny dát po presune sa do starej kópie neprenesú; naplánuj ich bezpečný návrat.'],
        'commands': ['pvesm status', 'systemctl --failed', 'qm list', 'pct list', 'timedatectl'],
        'warnings': ['Pôvodné dáta a zálohy zachovaj, kým neoveríš prevádzku aj obnovu.'],
        'wiki_slug': 'post-recovery-checklist',
    },
    {
        'id': 'retire-old', 'methods': ['disk_move', 'side_by_side'],
        'title': 'Vyradenie starého servera', 'goal': 'Uzavrieť migráciu po úspešnom cykle záloh a skúške obnovy.',
        'tasks': ['Nechaj nový host prejsť aspoň jedným cyklom záloh a over obnovu jedného hosťa.',
                  'Over vypnuté backup joby, timery a autostart na starom; starý server vypni.',
                  'Stiahni novú offline príručku; staré disky vymaž až po vedomom rozhodnutí.'],
        'commands': [],
        'warnings': ['Vymazanie starých diskov zruší cestu späť. Appka žiadne mazanie ani migráciu nevykonáva.'],
        'wiki_slug': 'hw-migration',
    },
]

# ---------------------------------------------------------------------------
# Hlavný recovery checklist (New HW)
# ---------------------------------------------------------------------------

RECOVERY_CHECKLIST = [
    {
        'step': 1,
        'id': 'install-pve',
        'title': 'Nainštalovať čistý Proxmox VE',
        'goal': 'Funkčný čistý host v rovnakej major verzii ako pôvodný.',
        'tasks': [
            'Rovnaká MAJOR verzia ako pôvodný server (pveversion-v.txt).',
            'Pôvodný hostname (FQDN) a ideálne pôvodná management IP.',
            'Rovnaký typ root storage (ext4/LVM vs. ZFS).',
        ],
        'commands': ['pveversion -v'],
        'wiki_slug': 'new-hardware',
    },
    {
        'step': 2,
        'id': 'check-hardware',
        'title': 'Skontrolovať nový hardvér',
        'goal': 'Vedieť, čím sa nový HW líši od starého.',
        'tasks': ['Názvy NIC a MAC', 'Disky, disk ID a UUID', 'CPU (Intel/AMD)', 'Storage layout', 'APT repozitáre podľa verzie'],
        'commands': ['ip -br link', 'lsblk -f', 'ls -l /dev/disk/by-id/', 'lscpu | head -20', 'lspci -nn'],
        'wiki_slug': 'new-hardware',
    },
    {
        'step': 3,
        'id': 'mgmt-network',
        'title': 'Nastaviť minimálnu management sieť',
        'goal': 'Dostať nový server do siete a zabezpečiť prístup k backup storage.',
        'tasks': ['Jeden bridge s management IP a gateway', 'Ak bola management VLAN, nastav ju ručne', 'DNS', 'Starý interfaces zatiaľ neaplikuj'],
        'commands': ['ifreload -a', 'ip -br addr', 'ip route', 'ping -c3 <gateway>'],
        'wiki_slug': 'network',
    },
    {
        'step': 4,
        'id': 'mount-backup-storage',
        'title': 'Pripojiť QNAP / WD / backup storage',
        'goal': 'Mať prístup k archívu hosta aj k vzdump zálohám VM/LXC.',
        'tasks': ['Nainštalovať nfs-common (autofs)', 'Povoliť IP nového hosta na NAS', 'Dočasný ručný mount alebo autofs mapy z archívu'],
        'commands': ['apt install -y nfs-common autofs', 'showmount -e <NAS-IP>', 'mount -t nfs <NAS-IP>:/<export> /mnt/restore-nas'],
        'wiki_slug': 'autofs-nas',
    },
    {
        'step': 5,
        'id': 'load-host-backup',
        'title': 'Načítať backup pôvodného hosta',
        'goal': 'Archív rozbalený v bezpečnom review adresári, nie v /.',
        'tasks': ['Rozbaliť do /root/pve-restore-review (0700)', 'Prečítať backup-info/README-RESTORE.txt', 'Pozrieť snapshot hosta (NIC, disky, verzia)'],
        'commands': ['mkdir -p /root/pve-restore-review && chmod 700 /root/pve-restore-review', 'tar -xzf proxmox_backup_*.tar.gz -C /root/pve-restore-review'],
        'wiki_slug': 'host-snapshot',
    },
    {
        'step': 6,
        'id': 'compare-hw-config',
        'title': 'Porovnať HW-závislú konfiguráciu',
        'goal': 'Zistiť, čo sa dá prevziať a čo treba prepísať.',
        'tasks': ['/etc/network', '/etc/fstab', 'kernel moduly a modprobe', 'sysctl', '/etc/default (GRUB, IOMMU)', 'passthrough v VM configoch'],
        'commands': ['diff -u etc/network/interfaces /etc/network/interfaces', 'diff -u etc/fstab /etc/fstab', "grep -E 'hostpci|usb[0-9]' etc/pve/nodes/*/qemu-server/*.conf"],
        'wiki_slug': 'disks-fstab',
    },
    {
        'step': 7,
        'id': 'restore-pve-config',
        'title': 'Obnoviť PVE konfiguráciu',
        'goal': 'Mať pôvodné VM/LXC definície, storage, používateľov a joby.',
        'tasks': ['config.db: iba pri zastavenom pve-cluster, 0600, potom reboot', 'alebo selektívne súbory cez cp (bez -a) do bežiaceho /etc/pve', 'Nikdy cp -r backup/etc/pve /etc/pve'],
        'commands': ['systemctl stop pve-cluster', 'chmod 0600 /var/lib/pve-cluster/config.db', 'reboot'],
        'wiki_slug': 'pve-config-db',
        'danger': 'ADVANCED: /etc/pve je pmxcfs (FUSE nad config.db). Pred týmto krokom si prečítaj wiki.',
    },
    {
        'step': 8,
        'id': 'verify-identity-storage',
        'title': 'Skontrolovať identitu, storage a sieť',
        'goal': 'Host je tým pôvodným a vidí svoje storage.',
        'tasks': ['hostname', '/etc/hosts', 'storage.cfg vs. reálne disky', 'network a bridge', 'NFS/CIFS', 'users', 'firewall', 'datacenter config'],
        'commands': ['hostname --ip-address', 'pvesm status', 'ls /etc/pve/nodes/', 'pve-firewall status'],
        'wiki_slug': 'pve-config-db',
    },
    {
        'step': 9,
        'id': 'selective-host-config',
        'title': 'Selektívne obnoviť host-specific konfiguráciu',
        'goal': 'Vrátiť vlastné služby, skripty a prístupy – iba to, čo je tvoje.',
        'tasks': ['SSH', 'systemd jednotky a timery', 'cron', 'autofs', 'vlastné skripty', '/root', '/usr/local/bin', '/usr/local/sbin', 'vlastné účty (useradd s pôvodným UID)'],
        'commands': ['systemctl daemon-reload', 'sshd -t', 'crontab -e', 'systemctl restart autofs'],
        'wiki_slug': 'systemd-cron',
    },
    {
        'step': 10,
        'id': 'restore-vm-lxc',
        'title': 'Obnoviť VM a LXC',
        'goal': 'Bežiace hosťovské systémy z existujúcich diskov alebo vzdump/PBS/NAS záloh.',
        'tasks': ['Import ZFS/LVM, ak disky prežili', 'qmrestore / pct restore z NAS', 'Bridge, CPU typ, passthrough', 'Autostart až po overení'],
        'commands': ['zpool import', 'qmrestore <archív> <vmid> --storage <storage>', 'pct restore <vmid> <archív> --storage <storage>'],
        'wiki_slug': 'vm-lxc',
    },
    {
        'step': 11,
        'id': 'post-recovery',
        'title': 'Post-recovery kontrola',
        'goal': 'Overiť, že všetko beží a zálohovanie je opäť funkčné.',
        'tasks': ['pveversion', 'pvesm status', 'ip addr / ip route', 'mount / findmnt', 'systemctl --failed', 'VM/LXC status', 'storage a NFS/CIFS', 'backup joby', 'firewall', 'DNS', 'čas/NTP', 'nová záloha hosta'],
        'commands': ['pveversion', 'pvesm status', 'ip addr', 'ip route', 'findmnt', 'systemctl --failed', 'qm list; pct list', 'timedatectl'],
        'wiki_slug': 'post-recovery-checklist',
    },
]
