# Azure Review CLI

Azure DevOps pull request süreçlerini hızlandırmak için hazırlanmış bir CLI aracı.

## Özellikler

- Birden fazla `project/repository` hedefinde aktif PR'ları listeler
- `check` akışı:
  - Unresolved comment thread kontrolü yapar
  - İlgili pipeline/build sonucunu kontrol eder
  - Pipeline içinde sadece belirli stage adını (`TEST_PIPELINE`) değerlendirir
  - Thread + pipeline uygunsa approve adımına geçer
  - PR zaten sizin tarafınızdan approve edilmişse tekrar sormaz
- `review` akışı:
  - PR diff'ini dosya bazında renkli gösterir
  - `-ai/--ai` ile mock AI review çıktısı verir
- `comment` akışı:
  - PR'a top-level yorum ekler
- `-log/--log` ile yapılan işlem adımları ve tüm HTTP istekleri anlık loglanır

Not: Merge işlemi bilinçli olarak devre dışı bırakılmıştır.

## Gereksinimler

- Python 3.9+
- Paketler:
  - `click`
  - `requests`
  - `rich`

Kurulum:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install click requests rich
```

## Konfigürasyon (.env)

Proje köküne `.env` dosyası oluşturun:

```env
ORGANIZATION=YourOrganization
PAT=YOUR_AZURE_DEVOPS_PAT
PROJECT_REPOS={"PROJECT_NAME1":["repo1","repo2"],"PROJECT_NAME2":["repo3"]}
TARGET_USERS=["Name Surname", "Another User"]
TEST_PIPELINE=Test
```

### Değişkenler

- `ORGANIZATION`: Azure DevOps organization adı
- `PAT`: Azure DevOps PAT
- `PROJECT_REPOS`: `project -> repo listesi` JSON map'i
- `TARGET_USERS`: PR listesinde kullanıcı filtresi (JSON array veya virgülle ayrılmış string)
- `TEST_PIPELINE`: `check` sırasında kontrol edilecek stage adı (varsayılan: `Test`)

## Kullanım

### Komut formatı

```bash
python3 main.py <command> [options]
```

Desteklenen komutlar:

- `list`
- `check`
- `review`
- `comment`

### 1) Aktif PR listesi

```bash
python3 main.py list
python3 main.py list -u "Name Surname"
python3 main.py list -log
```

Kısa alias:

```bash
python3 main.py -l
python3 main.py -list
```

### 2) PR check + approve

```bash
python3 main.py check 12345
python3 main.py check 12345 -log
```

Kısa alias:

```bash
python3 main.py -c 12345
python3 main.py -check 12345
python3 main.py -c -log 12345
```

### 3) PR diff review

```bash
python3 main.py review 12345
python3 main.py review 12345 -ai
python3 main.py review 12345 -log
```

Kısa alias:

```bash
python3 main.py -r 12345
python3 main.py -r -ai 12345
python3 main.py -r -log 12345
```

### 4) PR'a yorum ekleme

```bash
python3 main.py comment 12345 "test comment"
python3 main.py comment 12345 "test comment" -log
```

Kısa alias:

```bash
python3 main.py -cm 12345 "test comment"
python3 main.py -comment 12345 "test comment"
python3 main.py -cm -log 12345 "test comment"
```

## Check Çıktısı

`check` komutu comment ve pipeline durumlarını ayrı satırlarda gösterir:

- `Comment: ...`
- `Pipeline: ...`

İkisi de başarılıysa ayrıca:

- `All threads are resolved`

mesajı gösterilir ve approve adımı devam eder.

## Güvenlik

- `PAT` bilgisini sadece `.env` içinde tutun.
- `.env`, `.gitignore` içinde olmalıdır.
