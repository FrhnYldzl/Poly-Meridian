# Claude Code Kickoff — Poly Meridian

**Kullanım:** Bu metni olduğu gibi `claude code` CLI'ye veya yeni bir Claude Code oturumuna yapıştır. Tek başına yeterlidir; tüm detaylar `POLY_MERIDIAN_MASTER.md`'de.

---

Sen Claude Code'sın ve sıfırdan bir **Polymarket quant trading agent** ("Poly Meridian") inşa edeceksin. Bu fonu Renaissance/Medallion mantığında çalıştırmak istiyoruz: çok-stratejili hibrit, kategori-agnostik, paper trading'den kademeli canlıya.

## ZORUNLU İLK ADIM
Aynı klasörde `POLY_MERIDIAN_MASTER.md` adında bir master spec var. **Önce onu baştan sona oku.** Sonra repo köküne `docs/MASTER_SPEC.md` olarak kopyala. Bundan sonra her kararını bu spec'e referansla ver.

## TEKNİK BAĞLAM
- Python 3.12, uv ile dep mgmt
- Docker Compose ile servis orkestrasyonu (Postgres+TimescaleDB, Redis, agent, Prometheus, Grafana)
- pydantic v2 (config + models), structlog, pytest + pytest-asyncio
- asyncio her yerde I/O için
- mypy --strict ve ruff (E,F,I,UP,B,SIM), black

## DEĞİŞMEZ KURALLAR
1. **MODE=paper** default; live'a yalnızca `scripts/promote_to_live.py` checklist'i ile geçilir.
2. Secrets asla repo'ya commit edilmez; sadece `.env.example`.
3. Risk engine her emrin önündedir — bypass edecek kod yok.
4. Her yeni modülde önce ABC/Protocol, sonra implementation.
5. Her PR'da: ruff + black + mypy + pytest geçmeli.

## ÇALIŞMA DÜZENİ (Master Spec §24'teki Phase'leri takip et)

**Phase 0 — Setup** (önce bunu bitir, sonra dur ve özet ver):
- Repo iskeleti (`POLY_MERIDIAN_MASTER.md` §10 dosya yapısına birebir uy)
- `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `Makefile`
- DB schema migration'ı (`POLY_MERIDIAN_MASTER.md` §12)
- Boş modül ABC'leri: `BaseStrategy`, `RiskPolicy`, `Executor`, `IngestionSource`
- `bootstrap_db.sh` ile TimescaleDB hypertable'ları kurulu
- `make up` ile tüm servisler ayağa kalkıyor; `make test` boş test suite'i geçiyor

**Phase 1, 2, 3, 4, 5, 6:** Master Spec §24'te tanımlı sırayla. Her phase sonunda `STATUS.md` güncelle.

## BAŞLANGIÇ KOMUTU
```bash
# 1) Phase 0'a başla, dosya yapısını oluştur
# 2) Master spec §10'a tam uy
# 3) Hiç implementation kodu yazma — sadece iskelet + ABC + DB + Docker
# 4) Phase 0 bitince DURDU ve özet ver (neler yapıldı, neler test edildi)
# 5) Onayımı al, Phase 1'e geç
```

Her sorduğun soruda Master Spec'in ilgili bölüm numarasına atıfta bulun (örn. "§14.2 SentimentStrategy için..."). Belirsizlik varsa Master Spec'i tekrar oku, sonra net teknik soruyla bana dön — varsayım yapma.

Hadi başla.
