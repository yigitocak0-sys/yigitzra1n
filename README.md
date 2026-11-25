# yigitzra1n — iOS 16.7.16 (`20H392`) for iPhone 6s (`N71mAP` / TSMC A9 `s8003`)

[**🇬🇧 English**](#english) | [**🇹🇷 Türkçe**](#türkçe)

---

<a id="english"></a>

## 🇬🇧 English Documentation

An end-to-end, open-source build & tethered boot toolchain (**yigitzra1n**) that ports **iOS / iPadOS `16.7.16 (20H392)`** onto the **iPhone 6s (`iPhone8,1` / `N71mAP`, TSMC A9 `s8003`)** using only stock official Apple IPSW downloads and locally compiled patches.
> **No Apple binaries, no private keys, and no proprietary third-party binaries are distributed in this repository.** Every firmware image is constructed locally on your Mac from official Apple CDN IPSWs.

### Hardware & Software Requirements

- **Target Device:** iPhone 6s (`iPhone8,1`) with **TSMC A9 (`N71mAP`, CPID `0x8003`)**.
  - **Samsung A9 (`N71AP`, CPID `0x8000`)** is blocked by default to avoid boot loops; experimental users can pass `--force`.
- **Required Starting OS on Device:** **iOS `15.8.8 (19H422)`** must be installed and activated on your device before running `install` (provides the signed A9 bootchain/SEP baseline). Make sure to sign out of Apple ID / iCloud in Settings before starting.
- **Working Directory (Important):** Run the project from a local folder that is **not** managed by iCloud Drive (e.g., `~/Downloads` rather than `~/Desktop` or `~/Documents`) so macOS does not offload large IPSW files during build.
- **USB Cable (Important):** **Avoid USB-C to Lightning cables** — they frequently fail to enter DFU mode on A9/checkm8 devices. Use a **USB-A to Lightning** cable (with a USB-A to USB-C adapter/hub if your Mac only has USB-C ports).
- **Host Machine:** macOS (Apple Silicon or Intel) with:
  - **Xcode Command Line Tools** (`xcode-select --install`, ~500 MB) — **full 15 GB Xcode is NOT required** (if installed, its `iPhoneOS` SDK is used automatically; otherwise `prepare_ios_sdk.py` builds a lightweight iOS stub SDK from `CommandLineTools`).
  - Homebrew packages:
    ```bash
    brew install zstd libimobiledevice libirecovery libusb
    brew install hudochenkov/sshpass/sshpass
    ```
  - ~35 GB free disk space during image construction.

### Working vs. Known Hardware Limitations

| Subsystem | Status | Notes |
|---|---|---|
| Display (`750×1334`), Touch & 3D Touch | Working | `AppleMultitouchSPIC1F15` `C1,N71,2` + Chestnut power init |
| Wi-Fi & Bluetooth | Working | `hw_init_probe` D2255 `WLAN_REG_ON` / `BT_REG_ON` + 6s firmware |
| Speaker, Taptic Engine, Mic & Audio | Working | `AppleCS35L19Amp` & `AppleCS42L71Audio` relinked from 15.8.8 + DT v5 mic fix + N71m tunings |
| Camera, Flash & Preferences | Working | `D2255` LDO8/LDO21 + `CMCapture/{N71,N71m}` + `libn71m_avgestalt.dylib` |
| Battery, 1A Charging & Boot Clock | Working | `sn2400_charger_daemon` v6 + `AppleARMPMU` Site 1–3 + AOP auto-load & `mediaserverd` refresh |
| iPhone Keyboard, Wallpapers & Tips.app | Working | `KBLayouts_iPhone.dat` + standalone `WatchFacesWallpaperSupport` + `libn71m_tipsmodel.dylib` |
| Local Activation & Initial Setup | Working | `./yigitzra1n.sh activate` over USB/SSH |
| USB & Wi-Fi Root SSH (`alpine`) | Working | Procursus OpenSSH (`sshd` / SFTP port 22, native `ios16_sshd_launcher`) + Maintenance SSH RamDisk |
| **SEP / Touch ID / Apple Pay / Passcode** | **Not supported** | 15.8.8 SEP is incompatible with iOS 16 kernel (`sepfw-load-at-boot=0`) |
| **Cellular Baseband** | **Not supported** | Donor OS is iPadOS 16.7.16; baseband volume is excluded |
| **Tethered Boot** | **Required** | Device requires `./yigitzra1n.sh boot` from DFU mode after every full power-off |

### Quick Start (3 Commands)

#### 1. Prepare All Patched Images (No Device Needed)

Place (or symlink with `ln -s`) the 3 stock Apple IPSWs into `./ipsw/`, **or** let the script download them automatically from Apple's official CDN:

- **iPhone 6s `15.8.8 (19H422)`:** `iPhone_4.7_15.8.8_19H422_Restore.ipsw`
- **iPad 5 `16.7.16 (20H392)`:** `iPad_64bit_TouchID_ASTC_16.7.16_20H392_Restore.ipsw`
- **iPhone 8 `16.7.16 (20H392)`:** `iPhone_4.7_P3_16.7.16_20H392_Restore.ipsw`

```bash
chmod +x yigitzra1n.sh scripts/*.sh scripts/*.py
./yigitzra1n.sh prepare
```

This single command automatically:
1. Sets up `.venv` (`pyimg4`, `capstone`, `remotezip`) and verifies host tools.
2. Downloads any missing IPSWs into `ipsw/` (with resume support) and fetches open-source Procursus OpenSSH packages.
3. Compiles `hw_init_probe.bin` (PongoOS kext + embedded `yigitzra1n` boot logo), `sn2400_charger_daemon` v6, `ios16_sshd_launcher`, `ssh_rd_init`, `rd_reset_apfs`, `ios16_skip_setup`, `libn71m_avgestalt.dylib`, `libn71m_tipsmodel.dylib`, `sleepd_stub`, `soft_aks.o`, and `fast_dns.dylib`.
4. Builds **KernelCache v6** (23 base patches + `AppleMultitouchSPIC1F15` + `AppleCS35L19Amp` & `AppleCS42L71Audio` kext relinks + `AppleARMPMU` Site 1–3 + AOP v6 + 49 KB KPP `extra`).
5. Builds **DeviceTree v5** (Touch, Wi-Fi, BT, Camera, Audio, internal Mic fix, CHRP NVRAM, SEP-less flags).
6. Builds both the **14-patch Restore RamDisk** and the **Maintenance OpenSSH RamDisk** (+ TrustCaches, plus dedicated `engine_ramdisk` booter).
7. Assembles the **Hybrid RootFS (`098-68805-067.dmg`)** with merged `Cryptex1,SystemOS/AppOS`, 6s firmware/camera/audio profiles, iPhone 8 UI/keyboard/wallpaper assets, `securityd` (`soft_aks`) & `locationd` patches, signed OpenSSH, and `asr imagescan`.

#### 2. Install onto Your iPhone 6s & Activate Device (3-Step Guided DFU Flow)

Connect your iPhone 6s booted normally into **iOS 15.8.8** (unlocked on the Home Screen, Apple ID signed out), then run:

```bash
./yigitzra1n.sh install
```

Follow the guided DFU prompts (supports automatic checkpoint resume if interrupted):
1. **STEP 1/3 — DFU #1 (Device SHC Block):** Captures the A9 SHC block from iOS `15.8.8` (reused for both restore and daily SEP-less tethered boots).
2. **STEP 2/3 — DFU #2 (NAND Restore):** Writes the hybrid iOS `16.7.16` RootFS & bootchain to NAND.
3. **STEP 3/3 — First Boot:** Boots straight into iOS `16.7.16` using the captured SHC block.

*(Tip: During DFU exploitation, if progress pauses for >10 seconds, unplug and replug the Lightning cable for 1 second to wake the USB state machine.)*

Once installation finishes and the device boots into iOS `16.7.16`, run the activation command to complete setup and drop onto the Home Screen:

```bash
./yigitzra1n.sh activate
```

#### 3. Daily Tethered Boot & Maintenance SSH RamDisk

Whenever the device powers off completely, enter DFU mode and run:

```bash
./yigitzra1n.sh boot          # verbose kernel logs + centered yigitzra1n logo (default)
./yigitzra1n.sh boot --quiet  # hide kernel console text (-v)
```

To enter the maintenance SSH RamDisk at any time (mounts NAND volumes read-write into `/mnt1` and `/mnt2` and starts OpenSSH on port `22`, default password `alpine`):

```bash
./yigitzra1n.sh ssh-ramdisk
```

To refresh userland patches over USB/Wi-Fi SSH (or install your own device's legitimate `IC-Info.sisv` backed up from iOS 15.8.8):

```bash
./yigitzra1n.sh patch-device [--ic-info /path/to/IC-Info.sisv]
```

#### 4. Returning to Stock iOS 15.8.8 (One-Command Error 9 Prevention)

Because `yigitzra1n` runs SEP-less with an unencrypted `Data` volume, stock iOS `15.8.8` `RestoreRamDisk` will panic during `Checking for uncollected logs (44)` with `Error 9` (`"unencrypted data volume is not allowed" @apfs_vfsops.c:2445`) if you try to restore directly over it.

Run the built-in uninstall helper to cleanly reset **only** the APFS container (`/dev/disk0s1` — raw `/dev/disk0`, NOR, and Syscfg are untouched) and prepare your iPhone 6s for a clean stock restore:

```bash
./yigitzra1n.sh uninstall
```

---

<a id="türkçe"></a>

## 🇹🇷 Türkçe Dokümantasyon

**yigitzra1n**, resmi Apple IPSW dosyalarını ve yerel olarak derlenen yamaları kullanarak **iOS / iPadOS `16.7.16 (20H392)`** sürümünü **iPhone 6s (`iPhone8,1` / `N71mAP`, TSMC A9 `s8003`)** üzerinde çalıştıran uçtan uca açık kaynaklı bir imaj oluşturma ve tethered boot aracıdır.
> **Bu depoda hiçbir Apple ikili dosyası (binary), gizli anahtar veya sahipli üçüncü taraf ikili dağıtılmaz.** Tüm sistem imajları Mac'inizde doğrudan Apple'ın resmi sunucularından indirilen stok IPSW dosyalarından yerel olarak üretilir.

### Donanım ve Yazılım Gereksinimleri

- **Hedef Cihaz:** **TSMC A9 (`N71mAP`, CPID `0x8003`)** işlemcili iPhone 6s (`iPhone8,1`).
  - **Samsung A9 (`N71AP`, CPID `0x8000`)** cihazlar açılış döngüsünü önlemek için varsayılan olarak engellenmiştir; deneysel kullanım için `--force` bayrağı verilebilir.
- **Cihazda Kurulu Olması Gereken Başlangıç Sürümü:** Kuruluma (`install`) başlamadan önce cihazda **iOS `15.8.8 (19H422)`** kurulu ve etkinleştirilmiş olmalıdır. İşleme başlamadan önce cihazın Ayarlar menüsünden Apple ID / iCloud hesabınızdan çıkış yapın.
- **Çalışma Klasörü (Önemli):** macOS iCloud Drive'ın büyük IPSW dosyalarını buluta taşıyıp (`dataless`) işlemi kesmesini önlemek için aracı `~/Desktop` (Masaüstü) veya `~/Documents` yerine **`~/Downloads` (İndirilenler)** gibi yerel bir klasörün içinden çalıştırın.
- **USB Kablosu (Önemli):** A9 cihazlarda DFU moduna geçerken **USB-C - Lightning** kabloları donanımsal olarak sık sık başarısız olur. Mutlaka **USB-A - Lightning** kablo (gerekirse USB-A -> USB-C dönüştürücü adaptör/hub ile) kullanın.
- **Bilgisayar (Host):** macOS (Apple Silicon veya Intel):
  - **Xcode Command Line Tools** (`xcode-select --install`, ~500 MB) — **15 GB'lık tam Xcode zorunlu değildir** (kuruluysa `iPhoneOS` SDK'sı kullanılır; değilse `CommandLineTools` içinden hafif bir iOS stub SDK otomatik oluşturulur).
  - Homebrew paketleri:
    ```bash
    brew install zstd libimobiledevice libirecovery libusb
    brew install hudochenkov/sshpass/sshpass
    ```
  - İmaj hazırlama aşaması için ~35 GB boş disk alanı.

### Çalışan Özellikler ve Bilinen Donanım Sınırları

| Alt Sistem | Durum | Açıklama |
|---|---|---|
| Ekran (`750×1334`), Dokunmatik ve 3D Touch | Çalışıyor | `AppleMultitouchSPIC1F15` `C1,N71,2` + Chestnut güç başlatma |
| Wi-Fi ve Bluetooth | Çalışıyor | `hw_init_probe` D2255 `WLAN_REG_ON` / `BT_REG_ON` + 6s firmware |
| Hoparlör, Taptic Engine, Mikrofon ve Ses | Çalışıyor | 15.8.8'den yeniden bağlanan `AppleCS35L19Amp` ve `AppleCS42L71Audio` + DT v5 mikrofon yaması |
| Kamera, Flaş ve Ayarlar | Çalışıyor | `D2255` LDO8/LDO21 + `CMCapture/{N71,N71m}` + `libn71m_avgestalt.dylib` |
| Pil Gösterimi, 1A Şarj ve Saat | Çalışıyor | `sn2400_charger_daemon` v6 + `AppleARMPMU` + AOP otomatik yükleme |
| iPhone Klavyesi, Duvar Kağıtları ve İpuçları | Çalışıyor | `KBLayouts_iPhone.dat` + bağımsız `WatchFacesWallpaperSupport` + `libn71m_tipsmodel.dylib` |
| Cihaz Etkinleştirme ve Kurulum Tamamlama | Çalışıyor | USB/SSH üzerinden `./yigitzra1n.sh activate` |
| USB ve Wi-Fi Root SSH (`alpine`) | Çalışıyor | Procursus OpenSSH (`sshd` / SFTP port 22, `ios16_sshd_launcher`) + Bakım SSH RamDisk |
| **SEP / Touch ID / Apple Pay / Parola** | **Desteklenmiyor** | 15.8.8 SEP sürümü iOS 16 çekirdeğiyle uyumsuzdur (`sepfw-load-at-boot=0`) |
| **Hücresel Şebeke (Baseband / SIM)** | **Desteklenmiyor** | Donör sistem iPadOS 16.7.16 olduğu için baseband devre dışıdır |
| **Tethered Boot (Bilgisayarla Açılış)** | **Gerekli** | Cihazın şarjı bittiğinde veya tamamen kapatıldığında DFU modunda `./yigitzra1n.sh boot` çalıştırılmalıdır |

### Hızlı Başlangıç (3 Adımda Kurulum)

#### 1. Yamalı İmajları Hazırlama (Cihaz Bağlı Olmasına Gerek Yoktur)

Gerekli 3 stok Apple IPSW dosyasını `./ipsw/` klasörüne koyabilir ya da betiğin Apple sunucularından otomatik indirmesini sağlayabilirsiniz:

- **iPhone 6s `15.8.8 (19H422)`:** `iPhone_4.7_15.8.8_19H422_Restore.ipsw`
- **iPad 5 `16.7.16 (20H392)`:** `iPad_64bit_TouchID_ASTC_16.7.16_20H392_Restore.ipsw`
- **iPhone 8 `16.7.16 (20H392)`:** `iPhone_4.7_P3_16.7.16_20H392_Restore.ipsw`

```bash
chmod +x yigitzra1n.sh scripts/*.sh scripts/*.py
./yigitzra1n.sh prepare
```

*(Not: Hibrit RootFS oluşturulurken dosya sahipliklerinin `root:wheel` olarak korunması için Mac kullanıcı parolanız (`sudo`) istenecektir.)*

#### 2. iPhone 6s'e Kurulum ve Etkinleştirme

iPhone 6s cihazınızı **iOS 15.8.8** açık, ekran kilidi açılmış (Ana Ekranda), bilgisayara "Güven" verilmiş ve **Apple ID / iCloud hesabından çıkış yapılmış** şekilde USB-A - Lightning kablo ile bağlayın ve şu komutu çalıştırın:

```bash
./yigitzra1n.sh install
```

Ekrandaki yönlendirmeli DFU sayacını (`Guided DFU Helper`) takip edin:
1. **STEP 1/3 — DFU #1 (SHC Boot Bloğu):** iOS `15.8.8` üzerinden cihaza özel A9 SHC bloğunu yakalar ve kaydeder. *(DFU zamanlamasını kaçırırsanız araç cihazı otomatik olarak tekrar Recovery moduna alıp yeniden denemenizi sağlar.)*
2. **STEP 2/3 — DFU #2 (NAND Restore):** Hazırlanan hibrit iOS `16.7.16` sistemini cihaza yazar.
3. **STEP 3/3 — İlk Açılış (First Boot):** Cihazı doğrudan iOS `16.7.16` ile başlatır.

*(İpucu: DFU aşamasında ilerleme 10 saniyeden fazla duraklarsa USB kablosunu 1 saniyeliğine çıkarıp geri takın.)*

Cihaz iOS `16.7.16` ile açıldıktan sonra kurulum ekranını tamamlamak ve doğrudan Ana Ekran'a geçmek için şu komutu çalıştırın:

```bash
./yigitzra1n.sh activate
```

#### 3. Günlük Tethered Boot ve Bakım Komutları

Cihaz tamamen kapandığında veya yeniden başlatıldığında DFU moduna alıp açmak için:

```bash
./yigitzra1n.sh boot          # akan çekirdek yazıları + ortalanmış yigitzra1n logosu (varsayılan)
./yigitzra1n.sh boot --quiet  # çekirdek yazılarını gizle
```

Bakım amaçlı OpenSSH RamDisk'i başlatmak için (NAND bölümlerini `/mnt1` ve `/mnt2` altına bağlar, USB üzerinden `2222` portundan `root` / şifre `alpine` ile SSH erişimi açar):

```bash
./yigitzra1n.sh ssh-ramdisk
```

Canlı cihaz üzerindeki sistem yamalarını SSH üzerinden yenilemek için:

```bash
./yigitzra1n.sh patch-device
```

#### 4. Stok iOS 15.8.8'e Geri Dönüş (Tek Komutla Hata 9 Çözümü)

`yigitzra1n` SEP olmadan şifresiz bir `Data` bölümüyle çalıştığı için, doğrudan Finder veya iTunes üzerinden stok iOS `15.8.8` yüklemeye çalışırsanız Apple'ın `RestoreRamDisk`'i `Error 9` (`unencrypted data volume is not allowed`) hatası verir.

Stok iOS 15.8.8'e hatasız dönmek için önce şu komutu çalıştırın:

```bash
./yigitzra1n.sh uninstall
```

Bu komut Bakım SSH RamDisk'ini başlatır, yalnızca APFS konteynerini (`/dev/disk0s1`) temizler, cihazı Recovery moduna alır ve ardından Finder/iTunes veya `idevicerestore` ile **Hata 9 almadan** stok iOS 15.8.8 kurmanıza olanak tanır.

---

## Repository Layout / Klasör Yapısı

- `yigitzra1n.sh` — Ana komut satırı aracı (`prepare | install | boot | ssh-ramdisk | uninstall | activate | patch-device | message | messages | update | clean`) — açılışta GitHub güncellemelerini otomatik uygular (`--no-update` ile atlanabilir)
- `tutorial.txt` — Adım adım Türkçe & İngilizce kurulum rehberi (Step-by-step plain-text guide)
- `config/firmware_urls.env` — Resmi Apple CDN bağlantıları ve açık kaynak paket adresleri
- `src/` — Tüm C / Objective-C / Python kaynak kodları (`hw_init_probe.c`, `bootlogo_data.h`, `sn2400_charger_daemon.c`, `ios16_sshd_launcher.c`, `ssh_rd_init.c`, `ios16_skip_setup.c`, `libn71m_avgestalt.m`, `libn71m_tipsmodel.m`, `sleepd_stub.c`, `soft_aks.c`, `fast_dns.c`, `local_server.py`)
- `scripts/` — Modüler derleme, kext bağlama, DeviceTree, RamDisk, RootFS ve önyükleme betikleri
- `entitlements/` — Sistem servisleri için ad-hoc imzalama yetki dosyaları

---

## Telemetry & Privacy / Telemetri ve Gizlilik

To help diagnose build/install failures and track security guard triggers, `yigitzra1n` sends a minimal, anonymous event via [`scripts/telemetry.sh`](scripts/telemetry.sh) on first launch (recorded locally in `.yigitzra1n_init` so it only fires once) and when a command succeeds, fails, or hits a hardware/safety guard:

- **What IS collected:** An irreversible 16-hex-char client hash (`client_id`, derived locally via 200,000-round `PBKDF2-HMAC-SHA256` from `IOPlatformSerialNumber` and truncated to 64 bits), event outcome (`first_run`, `install_success`, `install_fail`, `security_block`), subcommand & step name, failing script filename & line number, exit code, sanitized error summary, macOS version (`sw_vers`), CPU architecture (`arm64` / `x86_64`), and tool version.
- **What is NEVER collected:** No raw Mac serial number (hashed one-way locally and discarded immediately — never leaves your Mac), no iPhone ECID, UDID, or serial number, no IP address, Apple ID, or Mac username (`/Users/<name>` paths and long numeric IDs are stripped client-side before sending, and the server does not log IP addresses).
- **Opt-out:** Set `YIGITZRA1N_NO_TELEMETRY=1` before any command to completely disable telemetry:
  ```bash
  YIGITZRA1N_NO_TELEMETRY=1 ./yigitzra1n.sh prepare
  ```

*(Türkçe Özet)*: Kurulum/derleme hatalarını tespit edip aynı Mac'ten gelen denemeleri gruplayabilmek için yerel olarak **200.000 tur `PBKDF2-HMAC-SHA256` + 64-bit kırpma ile üretilen geri döndürülemez `client_id` özeti**, **başarı/hata sonucu, aşama, hata veren dosya/satır numarası, çıkış kodu ve macOS sürüm/mimari bilgisi** anonim olarak gönderilir. **Ham Mac seri numaranız (yerelde tek yönlü özetlenip atılır), iPhone ECID/UDID/seri numaranız, IP adresiniz veya kullanıcı adınız kesinlikle toplanmaz.** İstemezseniz `YIGITZRA1N_NO_TELEMETRY=1` ile tamamen kapatabilirsiniz.

---

## Credits & Acknowledgements

This project builds on the remarkable work of the iOS security & jailbreak research community. Huge thanks to:

- **turdus_m3rula** (`sep.lol` / `github.com/turdus-m3rula`) for the A9 tethered restore/boot runtime, `PongoOS` modules, and `libirecovery` / `idevicerestore` tooling that make A9 tethered booting possible.
- **checkra1n / PongoOS** teams for checkm8 and the extensible PongoOS pre-boot environment.
- **Procursus** (`apt.procurs.us`) for open-source ARM64 iOS userspace packages (`openssh`, `bash`, coreutils).
- **pyimg4** & **Capstone** maintainers for clean Image4 and ARM64 tooling.

## License

Original source code and scripts in this repository are released under the [MIT License](LICENSE). All Apple trademarks and firmware images belong to Apple Inc.
