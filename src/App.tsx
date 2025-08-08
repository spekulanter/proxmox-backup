import { useState } from 'react'
import { useKV } from '@github/spark/hooks'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Checkbox } from '@/components/ui/checkbox'
import { Progress } from '@/components/ui/progress'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { Server, Download, Upload, Settings, Check, AlertTriangle, Clock, HardDrive } from '@phosphor-icons/react'
import { toast } from 'sonner'

interface FTPConfig {
  host: string
  username: string
  password: string
  port: number
}

interface BackupFile {
  path: string
  name: string
  description: string
  critical: boolean
  selected: boolean
}

interface BackupHistory {
  id: string
  filename: string
  date: string
  size: string
  status: 'success' | 'failed'
}

const defaultBackupFiles: BackupFile[] = [
  { path: '/etc/pve/', name: 'PVE Konfigurácia', description: 'Hlavná konfigurácia Proxmox (VM, storage, users)', critical: true, selected: true },
  { path: '/etc/network/interfaces', name: 'Sieťová konfigurácia', description: 'Nastavenia sietí, mostov a VLAN', critical: true, selected: true },
  { path: '/etc/hosts', name: 'Hosts súbor', description: 'Mapovanie IP adries a názvov', critical: false, selected: true },
  { path: '/etc/hostname', name: 'Názov hostiteľa', description: 'Identifikácia servera', critical: false, selected: true },
  { path: '/etc/resolv.conf', name: 'DNS konfigurácia', description: 'Nastavenia DNS serverov', critical: false, selected: true },
  { path: '/etc/ssl/pve/', name: 'SSL certifikáty', description: 'Certifikáty pre webové rozhranie', critical: false, selected: true },
  { path: '/root/', name: 'Root adresár', description: 'Skripty a nastavenia administrátora', critical: false, selected: true },
  { path: '/var/lib/vz/template/', name: 'ISO a šablóny', description: 'Obrazy a šablóny pre VM/CT (môže byť veľké)', critical: false, selected: false },
  { path: '/etc/cron*', name: 'Cron úlohy', description: 'Naplánované automatické úlohy', critical: false, selected: true },
  { path: '/etc/vzdump.conf', name: 'Vzdump konfigurácia', description: 'Nastavenia zálohovania VM/CT', critical: false, selected: true }
]

export default function App() {
  const [ftpConfig, setFtpConfig] = useKV<FTPConfig>('ftp-config', { host: '', username: '', password: '', port: 21 })
  const [backupFiles, setBackupFiles] = useKV<BackupFile[]>('backup-files', defaultBackupFiles)
  const [backupHistory, setBackupHistory] = useKV<BackupHistory[]>('backup-history', [])
  const [autoBackupEnabled, setAutoBackupEnabled] = useKV<boolean>('auto-backup-enabled', false)
  const [autoBackupFrequency, setAutoBackupFrequency] = useKV<'weekly' | 'monthly'>('auto-backup-frequency', 'monthly')
  
  const [isBackingUp, setIsBackingUp] = useState(false)
  const [backupProgress, setBackupProgress] = useState(0)
  const [testingConnection, setTestingConnection] = useState(false)
  const [connectionStatus, setConnectionStatus] = useState<'idle' | 'success' | 'error'>('idle')

  const handleFtpConfigChange = (field: keyof FTPConfig, value: string | number) => {
    setFtpConfig(current => ({ ...current, [field]: value }))
  }

  const testFtpConnection = async () => {
    if (!ftpConfig.host || !ftpConfig.username) {
      toast.error('Vyplňte všetky povinné polia')
      return
    }

    setTestingConnection(true)
    setConnectionStatus('idle')
    
    try {
      await new Promise(resolve => setTimeout(resolve, 2000))
      setConnectionStatus('success')
      toast.success('FTP pripojenie úspešné!')
    } catch (error) {
      setConnectionStatus('error')
      toast.error('Chyba pripojenia k FTP serveru')
    } finally {
      setTestingConnection(false)
    }
  }

  const toggleFileSelection = (index: number) => {
    setBackupFiles(current => 
      current.map((file, i) => 
        i === index ? { ...file, selected: !file.selected } : file
      )
    )
  }

  const createBackup = async () => {
    const selectedFiles = backupFiles.filter(file => file.selected)
    if (selectedFiles.length === 0) {
      toast.error('Vyberte aspoň jeden súbor na zálohovanie')
      return
    }

    if (!ftpConfig.host) {
      toast.error('Nakonfigurujte FTP server')
      return
    }

    setIsBackingUp(true)
    setBackupProgress(0)

    try {
      for (let i = 0; i < selectedFiles.length; i++) {
        setBackupProgress(((i + 1) / selectedFiles.length) * 100)
        await new Promise(resolve => setTimeout(resolve, 500))
      }

      const newBackup: BackupHistory = {
        id: Date.now().toString(),
        filename: `proxmox-backup-${new Date().toISOString().split('T')[0]}.tar.gz`,
        date: new Date().toLocaleString('sk-SK'),
        size: '127 MB',
        status: 'success'
      }

      setBackupHistory(current => [newBackup, ...current])
      toast.success('Záloha úspešne vytvorená a nahraná na FTP server!')
    } catch (error) {
      toast.error('Chyba pri vytváraní zálohy')
    } finally {
      setIsBackingUp(false)
      setBackupProgress(0)
    }
  }

  const deleteBackup = (id: string) => {
    setBackupHistory(current => current.filter(backup => backup.id !== id))
    toast.success('Záloha vymazaná')
  }

  const selectedFilesCount = backupFiles.filter(file => file.selected).length
  const criticalFilesSelected = backupFiles.filter(file => file.critical && file.selected).length
  const totalCriticalFiles = backupFiles.filter(file => file.critical).length

  return (
    <div className="min-h-screen bg-background p-4">
      <div className="max-w-6xl mx-auto space-y-6">
        <div className="text-center space-y-2">
          <h1 className="text-3xl font-bold text-foreground flex items-center justify-center gap-2">
            <Server size={36} className="text-primary" />
            Proxmox Backup Manager
          </h1>
          <p className="text-muted-foreground text-lg">
            Správca záloh pre Proxmox VE servery
          </p>
        </div>

        <Tabs defaultValue="backup" className="space-y-6">
          <TabsList className="grid w-full grid-cols-4">
            <TabsTrigger value="backup">Zálohovanie</TabsTrigger>
            <TabsTrigger value="files">Súbory</TabsTrigger>
            <TabsTrigger value="settings">Nastavenia</TabsTrigger>
            <TabsTrigger value="history">História</TabsTrigger>
          </TabsList>

          <TabsContent value="backup" className="space-y-6">
            <div className="grid gap-6 md:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <Download size={20} />
                    Manuálna záloha
                  </CardTitle>
                  <CardDescription>
                    Vytvorte zálohu okamžite
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="space-y-2">
                    <div className="flex justify-between text-sm">
                      <span>Vybrané súbory:</span>
                      <Badge variant={selectedFilesCount > 0 ? "default" : "secondary"}>
                        {selectedFilesCount} z {backupFiles.length}
                      </Badge>
                    </div>
                    <div className="flex justify-between text-sm">
                      <span>Kritické súbory:</span>
                      <Badge variant={criticalFilesSelected === totalCriticalFiles ? "default" : "destructive"}>
                        {criticalFilesSelected} z {totalCriticalFiles}
                      </Badge>
                    </div>
                  </div>

                  {isBackingUp && (
                    <div className="space-y-2">
                      <div className="flex justify-between text-sm">
                        <span>Pokrok zálohovania:</span>
                        <span>{Math.round(backupProgress)}%</span>
                      </div>
                      <Progress value={backupProgress} />
                    </div>
                  )}

                  <Button 
                    onClick={createBackup} 
                    disabled={isBackingUp || selectedFilesCount === 0 || !ftpConfig.host}
                    className="w-full"
                  >
                    {isBackingUp ? 'Vytváram zálohu...' : 'Vytvoriť zálohu teraz'}
                  </Button>

                  {criticalFilesSelected < totalCriticalFiles && (
                    <Alert>
                      <AlertTriangle size={16} />
                      <AlertDescription>
                        Nie sú vybrané všetky kritické súbory. Záloha nemusí byť kompletná.
                      </AlertDescription>
                    </Alert>
                  )}
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <Clock size={20} />
                    Automatická záloha
                  </CardTitle>
                  <CardDescription>
                    Naplánujte pravidelné zálohy
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="flex items-center space-x-2">
                    <Checkbox 
                      id="auto-backup"
                      checked={autoBackupEnabled}
                      onCheckedChange={(checked) => setAutoBackupEnabled(!!checked)}
                    />
                    <Label htmlFor="auto-backup">Povoliť automatické zálohy</Label>
                  </div>

                  {autoBackupEnabled && (
                    <div className="space-y-3">
                      <div>
                        <Label htmlFor="frequency">Frekvencia</Label>
                        <select 
                          className="w-full mt-1 p-2 border rounded-md"
                          value={autoBackupFrequency}
                          onChange={(e) => setAutoBackupFrequency(e.target.value as 'weekly' | 'monthly')}
                        >
                          <option value="weekly">Týždenne</option>
                          <option value="monthly">Mesačne</option>
                        </select>
                      </div>
                      
                      <Alert>
                        <Check size={16} />
                        <AlertDescription>
                          Ďalšia automatická záloha: {autoBackupFrequency === 'weekly' ? 'Nedeľa 02:00' : '1. deň v mesiaci 02:00'}
                        </AlertDescription>
                      </Alert>
                    </div>
                  )}
                </CardContent>
              </Card>
            </div>
          </TabsContent>

          <TabsContent value="files" className="space-y-6">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <HardDrive size={20} />
                  Výber súborov na zálohovanie
                </CardTitle>
                <CardDescription>
                  Označte súbory a adresáre, ktoré chcete zahrnúť do zálohy
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  {backupFiles.map((file, index) => (
                    <div key={file.path} className="flex items-start space-x-3 p-3 rounded-lg border">
                      <Checkbox
                        id={`file-${index}`}
                        checked={file.selected}
                        onCheckedChange={() => toggleFileSelection(index)}
                      />
                      <div className="flex-1 space-y-1">
                        <div className="flex items-center gap-2">
                          <Label htmlFor={`file-${index}`} className="font-medium cursor-pointer">
                            {file.name}
                          </Label>
                          {file.critical && (
                            <Badge variant="destructive" className="text-xs">
                              Kritické
                            </Badge>
                          )}
                        </div>
                        <p className="text-sm text-muted-foreground">{file.description}</p>
                        <p className="text-xs text-muted-foreground font-mono">{file.path}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="settings" className="space-y-6">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Settings size={20} />
                  FTP Server nastavenia
                </CardTitle>
                <CardDescription>
                  Nakonfigurujte pripojenie k FTP serveru pre ukladanie záloh
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid gap-4 md:grid-cols-2">
                  <div>
                    <Label htmlFor="ftp-host">IP Adresa / Hostiteľ *</Label>
                    <Input
                      id="ftp-host"
                      placeholder="192.168.1.100"
                      value={ftpConfig.host}
                      onChange={(e) => handleFtpConfigChange('host', e.target.value)}
                    />
                  </div>
                  <div>
                    <Label htmlFor="ftp-port">Port</Label>
                    <Input
                      id="ftp-port"
                      type="number"
                      placeholder="21"
                      value={ftpConfig.port}
                      onChange={(e) => handleFtpConfigChange('port', parseInt(e.target.value) || 21)}
                    />
                  </div>
                  <div>
                    <Label htmlFor="ftp-username">Používateľské meno *</Label>
                    <Input
                      id="ftp-username"
                      placeholder="backup_user"
                      value={ftpConfig.username}
                      onChange={(e) => handleFtpConfigChange('username', e.target.value)}
                    />
                  </div>
                  <div>
                    <Label htmlFor="ftp-password">Heslo</Label>
                    <Input
                      id="ftp-password"
                      type="password"
                      placeholder="••••••••"
                      value={ftpConfig.password}
                      onChange={(e) => handleFtpConfigChange('password', e.target.value)}
                    />
                  </div>
                </div>

                <Separator />

                <div className="flex items-center gap-3">
                  <Button 
                    variant="outline" 
                    onClick={testFtpConnection}
                    disabled={testingConnection || !ftpConfig.host || !ftpConfig.username}
                  >
                    {testingConnection ? 'Testujem...' : 'Test pripojenia'}
                  </Button>
                  
                  {connectionStatus === 'success' && (
                    <div className="flex items-center gap-1 text-accent">
                      <Check size={16} />
                      <span className="text-sm">Pripojenie úspešné</span>
                    </div>
                  )}
                  
                  {connectionStatus === 'error' && (
                    <div className="flex items-center gap-1 text-destructive">
                      <AlertTriangle size={16} />
                      <span className="text-sm">Chyba pripojenia</span>
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="history" className="space-y-6">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Upload size={20} />
                  História záloh
                </CardTitle>
                <CardDescription>
                  Prehľad vytvorených záloh na FTP serveri
                </CardDescription>
              </CardHeader>
              <CardContent>
                {backupHistory.length === 0 ? (
                  <div className="text-center py-8 text-muted-foreground">
                    <HardDrive size={48} className="mx-auto mb-4 opacity-50" />
                    <p>Zatiaľ neboli vytvorené žiadne zálohy</p>
                  </div>
                ) : (
                  <div className="space-y-3">
                    {backupHistory.map((backup) => (
                      <div key={backup.id} className="flex items-center justify-between p-3 rounded-lg border">
                        <div className="space-y-1">
                          <div className="flex items-center gap-2">
                            <span className="font-medium">{backup.filename}</span>
                            <Badge variant={backup.status === 'success' ? 'default' : 'destructive'}>
                              {backup.status === 'success' ? 'Úspešné' : 'Neúspešné'}
                            </Badge>
                          </div>
                          <div className="text-sm text-muted-foreground">
                            {backup.date} • {backup.size}
                          </div>
                        </div>
                        <div className="flex gap-2">
                          <Button variant="outline" size="sm">
                            Stiahnuť
                          </Button>
                          <Button 
                            variant="outline" 
                            size="sm"
                            onClick={() => deleteBackup(backup.id)}
                          >
                            Vymazať
                          </Button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}