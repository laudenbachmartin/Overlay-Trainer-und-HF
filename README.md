Trainings-Overlay
Ein kleines Windows-Programm für das Indoor-Training: Es zeigt Herzfrequenz, Leistung und Trittfrequenz als schwebendes, durchsichtiges Fenster über allen anderen Programmen, z. B. über MyWhoosh, Zwift, YouTube oder Netflix.
```
♥     152 Z4
♥ Ø   140
⚡     245
⚡ 3s  238
⚡ Ø   210
↻      90
```
Die Sensoren werden direkt per Bluetooth LE oder ANT+ (mit USB-Stick) gelesen. Das Programm braucht kein Internet und kein Konto.
---
Funktionen
Startmaske mit Kacheln für Herzfrequenz, Leistung und Trittfrequenz: Kachel anklicken, Gerät wählen, fertig
Bluetooth LE und ANT+ gemischt nutzbar (z. B. Gurt per ANT+, Trainer per Bluetooth)
Unterstützte Sensoren:
Herzfrequenz-Gurte (Bluetooth und ANT+)
Smarttrainer, z. B. Wahoo KICKR / KICKR Core (Bluetooth FTMS oder Cycling Power, ANT+ FE-C oder Power)
Wattmesser (Bluetooth Cycling Power, ANT+ Power)
Kadenzsensoren (Bluetooth CSC, ANT+ Cadence bzw. Speed/Cadence)
Overlay immer im Vordergrund, Hintergrund komplett durchsichtig, Mausklicks gehen durch die freien Flächen hindurch
Werte: HF, HF-Zone, Ø-HF, Watt, 3s-Watt, Ø-Watt, Trittfrequenz
Farben nach Zonen: HF nach 5 einstellbaren HF-Zonen, Watt nach den 7 Leistungszonen (nach Coggan) auf Basis der FTP
RPM-Alarm: Fällt die Trittfrequenz unter einen Mindestwert, wird sie rot und blinkt
Anordnung untereinander oder nebeneinander, Schriftgröße und Deckkraft einstellbar
Geräte und Einstellungen werden gespeichert und beim nächsten Start automatisch verbunden
---
Voraussetzungen
Was	Details
Windows	Windows 10 oder 11
Python	Version 3.11 oder neuer von https://www.python.org/downloads/ (beim Setup „Add python.exe to PATH“ anhaken)
Bluetooth	im PC eingebaut oder als USB-Adapter, muss Bluetooth LE (4.0+) können
ANT+ (optional)	ANT+-USB-Stick, z. B. Garmin USB-m, CYCPLUS, Anself. Dazu einmalig der Treiber WinUSB (siehe unten)
Sensoren	Bluetooth-LE- und/oder ANT+-fähige Geräte
---
Installation
Den Ordner `training-overlay` an einen festen Ort kopieren, z. B. `C:\training-overlay`.
PowerShell in diesem Ordner öffnen (im Explorer in die Adresszeile `powershell` tippen und Enter drücken).
Die benötigten Pakete installieren:
```
   py -m pip install -r requirements.txt
   ```
Starten:
```
   py overlay.py
   ```
Bequem starten (ohne Konsolenfenster)
Variante A: Verknüpfung (empfohlen)
Rechtsklick auf den Desktop → Neu → Verknüpfung
Als Speicherort eingeben (Pfad anpassen):
```
   pyw "C:\training-overlay\overlay.py"
   ```
Name „Trainings-Overlay“ → Fertig stellen. Die Verknüpfung kann auch an die Taskleiste angeheftet werden.
Variante B: `start_overlay.bat`
Doppelklick genügt. Beim ersten Start installiert die Datei fehlende Pakete selbst.
Hinweis: Windows Smart App Control blockiert heruntergeladene Batch-Dateien oft. Dann Variante A nehmen.
---
Erster Start
Kachel anklicken (Herzfrequenz, Leistung oder Trittfrequenz). Das Programm sucht 5 Sekunden nach Geräten.
Gerät in der Liste auswählen → Verbinden.
Bluetooth-Geräte stehen mit Signalstärke in der Liste, ANT+-Geräte mit „(ANT+)“.
Ein Trainer bei Leistung wird automatisch auch für Trittfrequenz eingetragen.
Beim KICKR über ANT+ gibt es zwei Einträge mit derselben Nummer. „Trainer (FE-C)“ nehmen.
Kachelfarben: grau = kein Gerät · orange = verbindet · grün = verbunden (mit Live-Wert) · rot = Problem (Meldung steht in der Kachel)
Optionen einstellen (siehe unten) → Weiter →. Das Overlay erscheint.
Optionen in der Startmaske
Option	Bedeutung
HFmax	maximale Herzfrequenz, Grundlage für die HF-Zonen
HF-Zonen …	Zonengrenzen Z2–Z5 in bpm einstellen. Standard: 60 / 70 / 80 / 90 % der HFmax
Min. RPM	darunter wird die Trittfrequenz rot und blinkt (0 = aus, bei 0 rpm/Rollen kein Alarm)
Anordnung	Werte untereinander oder nebeneinander
FTP	in Watt, Grundlage für die Farben der Leistungszonen
Zusätzlich	3s-Watt, Ø-Watt, Ø-HF, HF-Zone ein- oder ausblenden
Schriftgröße	50–300 %
---
Bedienung des Overlays
Aktion	Wirkung
Auf eine Zahl klicken und ziehen	Overlay verschieben
Mausrad über einer Zahl	größer/kleiner
Rechtsklick auf eine Zahl	Menü: Hintergrund an/aus, Anordnung wechseln, Durchschnitte zurücksetzen, Deckkraft, Schriftgröße, Geräte ändern, Beenden
Die freien Flächen zwischen den Zahlen sind durchsichtig und lassen Klicks zum Programm dahinter durch. Darum immer direkt auf eine Zahl klicken.
Beenden: Rechtsklick auf eine Zahl → Beenden, oder in der Startmaske auf das X. Notfalls im Task-Manager den Prozess Python / pythonw.exe beenden.
Farben
HF-Zone	Farbe		Leistungszone (% FTP)	Farbe
Z1 Erholung	grau		Z1 < 55 %	grau
Z2 Grundlage	blau		Z2 55–75 %	blau
Z3 Tempo	grün		Z3 75–90 %	grün
Z4 Schwelle	orange		Z4 90–105 %	gelb
Z5 Maximal	rot		Z5 105–120 %	orange
			Z6 120–150 %	rot
			Z7 > 150 %	lila
Die Farbe der aktuellen Watt richtet sich nach den 3s-Watt, damit sie nicht bei jedem Tritt flackert.
So wird gerechnet
3s-Watt: Mittelwert aller Messwerte der letzten 3 Sekunden
Ø-Watt: ein Wert pro Sekunde, Start beim ersten Tritt. Rollphasen danach zählen mit 0 W mit (wie bei Garmin/Strava).
Ø-HF: ein Wert pro Sekunde ab der ersten gemessenen HF
Neue Fahrt: Rechtsklick → Durchschnitte zurücksetzen
---
ANT+ einrichten (optional)
Über ANT+ können beliebig viele Empfänger gleichzeitig mitlesen. Beispiel: Der Trainer läuft per Bluetooth in MyWhoosh/Zwift, das Overlay liest ihn per ANT+ mit.
Einmalig: Treiber für den Stick auf WinUSB umstellen
Zadig herunterladen: https://zadig.akeo.ie
ANT+-Stick einstecken, Zadig starten, Options → List All Devices anhaken.
In der Auswahlliste den Stick wählen. Wichtig: Bei „USB ID“ muss `0FCF 1009` (oder `0FCF 1008`) stehen. Er heißt meist „ANT USBStick2“, „ANT USB-m Stick“ oder „Dynastream …“.
Nicht das falsche Gerät erwischen! Zur Kontrolle: Ziehst du den Stick ab, verschwindet der Eintrag.
Rechts WinUSB auswählen → Replace Driver.
Test:
```
py overlay.py --ant-test
```
Der Test prüft Pakete, Stick und Treiber und zeigt, welche ANT+-Geräte in der Nähe senden.
Gut zu wissen
Ein ANT+-Stick kann immer nur von einem Programm benutzt werden. MyWhoosh/Zwift verbinden die Sensoren dann per Bluetooth.
Andere Programme mit Garmin-ANT-Treiber (z. B. Garmin Express) sehen den Stick danach evtl. nicht mehr. Rückgängig machen: Geräte-Manager → Stick → Gerät deinstallieren (Haken „Treiber entfernen“) → neu einstecken.
Den Stick möglichst nah ans Rad legen (USB-Verlängerung).
---
Fehlerbehebung
Problem	Lösung
`No module named 'bleak'` / `'usb'`	`py -m pip install -r requirements.txt` ausführen
Installation von `bleak` schlägt fehl (sehr neue Python-Version)	zusätzlich Python 3.13 installieren: `py install 3.13`, dann `py -3.13 -m pip install -r requirements.txt` und `py -3.13 overlay.py`
Gerät taucht in der Suche nicht auf	Gerät wecken (Gurt anlegen/anfeuchten, kurz treten), Neu suchen, Haken „Alle Bluetooth-Geräte zeigen“ setzen
Gerät nur per Bluetooth gefunden, nicht per ANT+	`py overlay.py --ant-test` ausführen, Treiber prüfen (siehe ANT+)
Bluetooth-Gerät verbindet nicht, wenn MyWhoosh/Zwift läuft	Viele Trainer erlauben nur eine Bluetooth-Steuerverbindung. Overlay vor der Trainings-App starten, oder per ANT+ mitlesen
„Zugriff auf den Stick nicht möglich“	Treiber nicht WinUSB, oder ein anderes Programm (MyWhoosh, Zwift, Garmin Express) belegt den Stick
Kadenz zeigt `--`	Trainer liefert keine Kadenz (beim KICKR Core ist sie geschätzt, Firmware aktualisieren) oder separaten Kadenzsensor verwenden
Overlay verschwindet hinter dem Spiel	In der Trainings-App randloses Fenster statt exklusivem Vollbild einstellen
Gurt nicht in den Windows-Bluetooth-Einstellungen koppeln	Das Overlay verbindet sich direkt, Koppeln in Windows ist nicht nötig und kann stören
---
Dateien im Paket
Datei	Zweck
`overlay.py`	das Programm
`requirements.txt`	benötigte Python-Pakete (`bleak`, `pyusb`, `libusb-package`)
`start_overlay.bat`	Startdatei (installiert fehlende Pakete beim ersten Start)
`settings.json`	Einstellungen und gewählte Geräte. Wird automatisch angelegt und gespeichert
`README.md`	diese Anleitung
Zum Zurücksetzen aller Einstellungen `settings.json` löschen.
---
Technischer Hintergrund
Bluetooth LE über `bleak`. Profile: Heart Rate (0x180D), Fitness Machine/Indoor Bike Data (0x1826), Cycling Power (0x1818), Cycling Speed & Cadence (0x1816)
ANT+ direkt über USB (`pyusb` + `libusb`). Geräteprofile: Heart Rate (120), Bike Power (11), Fitness Equipment FE-C (17), Speed & Cadence (121), Cadence (122). Gerätesuche im Dauer-Scanmodus
Oberfläche mit `tkinter` (in Python enthalten)
Das Overlay liest nur, es steuert keinen Trainer und verändert keine Geräteeinstellungen
Keine Internetverbindung, keine Datenübertragung, alles bleibt lokal
