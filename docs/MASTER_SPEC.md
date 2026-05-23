# POLY MERIDIAN — Polymarket Quant Agent Master Documentation

> **Sürüm:** 1.1 · **Tarih:** Mayıs 2026 · **Dil:** Türkçe (kod ve API referansları İngilizce)
> **Hedef okuyucu:** (1) Fonu yöneten ekip — eğitim ve karar dokümanı olarak; (2) Claude Code — agent'ı sıfırdan inşa etmek için teknik spec olarak.
> **Stratejik karar matrisi (seçildi):** Çok-stratejili hibrit · Kategori-agnostik · Python + Docker + kendi sunucumuz · Paper trading → kademeli canlı.
> **Sürüm tarihçesi:** [`docs/CHANGELOG.md`](CHANGELOG.md) — her bump'ın değişen bölüm listesi.

---

## İÇİNDEKİLER

**BÖLÜM 1 — POLYMARKET'İ ANLAMAK (Girişimci/Fon yöneticisi bakışıyla)**
1. Polymarket nedir ve neden borsa gibi
2. Fiyat oluşumu, market mekaniği, ücretler
3. Hacim, likidite, kullanıcı tabanı — güncel rakamlar
4. Kim kazanıyor? Para nereden, hangi profilden geliyor
5. Edge'in beş kaynağı (bizim sömüreceğimiz)
6. Quant tarihi: Renaissance/Medallion mantığı bize ne öğretir
7. Gerçekçi getiri beklentileri ve fon ekonomisi
8. Hukuki çerçeve, vergi, risk haritası

**BÖLÜM 2 — POLY MERIDIAN MİMARİSİ (Claude Code için spec)**
9. Sistem genel bakış (high-level architecture)
10. Repo yapısı ve dosya envanteri
11. Veri katmanı (Data Ingestion)
12. Veritabanı şeması (PostgreSQL + TimescaleDB)
13. Feature engineering ve sinyal katmanı
14. Strateji katmanı (5 alt-strateji)
15. Risk motoru (Kelly + limitler + kill-switch)
16. Execution motoru (CLOB orders)
17. Portfolio manager
18. Backtest motoru
19. Paper trading modu
20. Observability (logging, metrics, alerting)
21. Konfigürasyon ve secrets
22. Deployment (Docker Compose)
23. Test stratejisi
24. Roadmap ve milestones
25. Claude Code'a verilecek build prompt (kopyala-yapıştır)

---

# BÖLÜM 1 — POLYMARKET'İ ANLAMAK

## 1. Polymarket nedir ve neden borsa gibi

Polymarket bir **prediction market** (tahmin piyasası) borsasıdır. Polygon blokzinciri üzerinde, USDC ile çalışır. İnsanlar gelecekte gerçekleşmesi belirsiz olaylar üzerine pozisyon alırlar:

> *"Trump 2028'de aday olacak mı?"*
> *"BTC 30 Haziran'a kadar $150K'yı geçecek mi?"*
> *"Liverpool şampiyon olacak mı?"*

Her olay (event) bir veya birkaç **market**'e bölünür. Her market'in iki tarafı vardır: **YES (Evet)** ve **NO (Hayır)**. Her taraf, $0.00 ile $1.00 arasında işlem gören bir "outcome token"dır.

**Temel kural:**
- YES + NO fiyatları toplamı her zaman ~$1.00 olmalıdır (arbitraj bunu zorlar).
- Olay gerçekleşirse kazanan taraf $1.00 olarak ödenir, kaybeden $0.00 olur.
- Yani $0.40'tan aldığınız bir YES, olay olursa $1.00 olur → %150 getiri. Olmazsa $0 → tam zarar.

**Neden borsa gibi?**
Polymarket arkada CLOB (Central Limit Order Book) çalıştırır. Yani Coinbase, Binance veya NASDAQ ile aynı mekanik:
- **Limit order**: belirlediğin fiyattan emir verirsin, eşleşene kadar bekler.
- **Market order**: anlık en iyi fiyattan alır/satar.
- **Order book**: bid/ask seviyeleri, market depth.
- **Maker/Taker**: order book'a likidite koyan vs. çeken.

Tek fark: pay senedi yerine *bir olayın olasılık fiyatını* trade ediyoruz. Bu, fiyatın $0 ve $1 arasında sıkışmasını sağlar (sınırlı oynaklık) ve **resolution date'de fiyat kesin sıfırlanır veya birlenir** — bu da hem fırsat hem risktir.

**Polymarket'in iki ayrı platformu var (2026 itibariyle):**
- **Polymarket International** (polymarket.com): KYC yok, USDC ile direkt, daha esnek.
- **Polymarket US** (Aralık 2025'te CFTC onayı ile yeniden açıldı): KYC zorunlu, regülasyonlu, ücret yapısı farklı.

Sources: [Polymarket Docs - Overview](https://docs.polymarket.com/), [Polymarket US CFTC Approval - Bulldog Law](https://www.thebulldog.law/polymarket-receives-cftc-approval-to-resume-us-operations-after-years-offshore)

---

## 2. Fiyat oluşumu, market mekaniği, ücretler

### 2.1 Fiyat = olasılık

Bir YES token'ı $0.65'ten işlem görüyorsa, *piyasanın kolektif tahmini* o olayın gerçekleşme olasılığının %65 olduğudur. Bu çok önemli bir kavram — biz "fiyat" derken aslında **iki şey** kastediyoruz:

1. **Market'in dediği olasılık** (mevcut fiyat)
2. **Bizim modelimizin dediği gerçek olasılık** (true probability)

**Edge = Bizim olasılığımız − Market olasılığı**

Eğer biz %80 dersek ve market %65 diyorsa, bizim için edge = +15 puan. Bu da pozitif beklenen değer (positive expected value, +EV) anlamına gelir.

### 2.2 Ücret yapısı (Mart 2026)

**Maker (limit order koyup bekleyen) → her zaman %0 ücret.**

**Taker (market order veya agresif limit order) → kategoriye göre değişir:**

| Kategori   | Taker Fee |
|------------|-----------|
| Crypto     | 1.80%     |
| Mentions   | 1.56%     |
| Economics  | 1.50%     |
| Culture    | 1.25%     |
| Weather    | 1.25%     |
| Finance    | 1.00%     |
| Politics   | 1.00%     |
| Tech       | 1.00%     |
| Sports     | 0.75%     |
| Geopolitics| 0% (ücretsiz) |

**Önemli detay:** Ücret, fiyatın 50¢'e yakınlığıyla *artar*. 50¢'te en yüksek, 1¢ veya 99¢'te en düşük. Yani belirsizliğin yüksek olduğu yerde ücret yüksek, belirginleşmiş marketlerde düşük.

**Polymarket US** (regülasyonlu): %0.30 flat taker fee, %0.20 maker rebate.

**Likidite Ödülleri (Maker Rewards):** Polymarket order book'u derinleştirmek için maker'lara günlük ödül dağıtır. Politics ve Tech'te taker fee'nin ~%25'i maker'a rebate olarak döner. Finance marketlerinde bu %50'ye kadar çıkabilir. Nisan 2026'da spor/esports marketlerine $5M+ likidite teşviki dağıtıldı.

**Bizim agent için anlamı:** Mümkün olduğunca **maker** ol. Edge'imiz dar ise (örn. %2), tek başına taker fee bunu yiyebilir. Limit order kullanıp order book'a likidite koymak hem ücret avantajı hem de rebate getirir.

Sources: [Polymarket Fees Docs](https://docs.polymarket.com/trading/fees), [Polymarket Help - Trading Fees](https://help.polymarket.com/en/articles/13364478-trading-fees), [Polymarket Liquidity Rewards](https://docs.polymarket.com/market-makers/liquidity-rewards)

### 2.3 Order tipleri

| Tip | Anlamı | Kullanım |
|-----|--------|----------|
| **GTC** (Good-Till-Cancelled) | Sen iptal edene kadar dururs | Sabırlı maker stratejisi |
| **GTD** (Good-Till-Date) | Belirli bir tarih/saate kadar geçerli | Resolution yaklaşırken |
| **FOK** (Fill-Or-Kill) | Tamamı dolar yoksa iptal | Tüm fırsat ya da hiç |
| **FAK** (Fill-And-Kill) | Mevcut kadarı al, kalanı iptal | Hızlı fırsat yakalama |

GTC/GTD = limit emir (maker olabilir). FOK/FAK = market emir (taker'dır).

Sources: [Polymarket Create Order Docs](https://docs.polymarket.com/developers/CLOB/orders/create-order)

### 2.4 Negatif risk marketler ve set arbitrajı

Bazı eventler "negatif risk" yapısındadır: 5 adaylı bir başkanlık marketinde 5 ayrı YES token vardır ve **toplamları $1.00 olmalıdır.** Eğer toplam $1.05'e çıkarsa (over-priced) hepsini shortlayıp $0.05 risksiz cebe atarsın. $0.95'e düşerse hepsini alıp $0.05 alırsın. **Bu, agent'ımızın ilk öğreneceği klasik bir arbitraj.**

Bir akademik çalışma 17,218 marketten **7,051'inde (%41) tek-market arbitrajı** tespit etti — bu çok ciddi bir verimsizlik.

Sources: [arXiv 2508.03474 - Probabilistic Forest Arbitrage](https://arxiv.org/abs/2508.03474)

---

## 3. Hacim, likidite, kullanıcı tabanı — güncel rakamlar

Polymarket 2024–2026 arasında patlama yaşadı. Rakamlar:

| Dönem            | Aylık Hacim | Not |
|------------------|-------------|-----|
| 2025 ortalama    | ~$1.2B      | 2024 ABD seçimi sonrası soğuma |
| Ekim 2025        | ~$3.0B      | Yeniden ivmelenme |
| Mart 2026        | **$10.57B** | İlk kez $10B eşiğini aştı |
| Nisan 2026       | $10.3B      | İlk %9'luk düşüş (Ağustos 2025'ten beri) |
| Q1 2026 toplam   | **~$26.2B** | Bir önceki çeyreğe göre +%90 |
| Aktif cüzdan (Şubat 2026) | 840,000 aylık | 6 ayda 3'e katlandı |

**Bunun bizim için anlamı:**
- Bazı marketlerde günlük hacim **milyonlarca dolar**, order book derin, slippage düşük.
- Ama uzun kuyruğun (long tail) içinde **on binlerce ince market** var; orada slippage yüksek, fiyat keşfi yavaş = **bizim avantajımız.**
- Kategoriler arası likidite çok eşitsiz: Politics > Crypto > Sports > Culture >>> Weather.

Sources: [Token Terminal - Polymarket Volume](https://tokenterminal.com/explorer/projects/polymarket/metrics/trading-volume), [Polymarket Tops $10B Monthly - BitKE](https://bitcoinke.io/2026/04/polymarket-in-march-2026/), [Polymarket Volume Falls 9% - Bloomberg](https://www.bloomberg.com/news/articles/2026-05-13/polymarket-trading-volume-declines-for-first-time-since-august), [TRM Labs - Prediction Markets $21B](https://www.trmlabs.com/resources/blog/how-prediction-markets-scaled-to-usd-21b-in-monthly-volume-in-2026)

---

## 4. Kim kazanıyor? Para nereden, hangi profilden geliyor

Polymarket'in **en güzel özelliği:** blokzincir = her cüzdan halka açık. Yani **kazananları gerçek zamanlı izleyebiliyoruz.** Bu, normal borsalarda asla mümkün değil.

### Efsane vakalar (case studies)

**"French Whale"** — 2024 ABD seçim döneminde 4 cüzdan kullanarak Trump'a ~$50M koydu ve **$85M** kâr yaptı. Sonradan ortaya çıktı: kamuoyu yoklamalarındaki gizli yanlılığı (response bias) sömürdü. Mantığı: anketler kadın seçmen ve şehir seçmenini olduğundan fazla temsil ediyor — bu temel bir model edge'i.

**Erasmus** — politika marketlerinde $1.3M+ profit. Yöntemi: ankete yakın takip + politika tartışmalarının ritmini okuma + kampanya momentum'u.

**HyperLiquid0xb** — $1.4M+ toplam profit, en büyük tek pozisyondan $755K.

**WindWalk3** — RFK Jr. sağlık politikaları üzerinden $1.1M+.

### Top traderların ortak özellikleri

1. **Uzmanlaşma.** Genelde tek bir kategoride dominate eder (politika, kripto, spor). Genelci olan az.
2. **Hız.** Yeni bilgi çıktığında order book'a ilk tepki veren onlar. Latency = para.
3. **Bilgi asimetrisi.** Niş alanlarda derin bilgi (örn. bir lobicinin Capitol Hill'deki temas ağı; bir kripto trader'ın on-chain veriyi okuyabilmesi).
4. **Risk yönetimi.** Bankrupt olanları biz görmüyoruz — sadece hayatta kalanları.
5. **Soğukkanlılık.** "Trade your edge, not your emotion."

### Bizim için kritik içgörü

> **Bu insanlar manuel çalışıyor. Bizim avantajımız: 24/7 tarama, çok-kaynaklı veri füzyonu, makine hızında karar verme.**

Üst-trader cüzdanlarını gerçek zamanlı izlemek (wallet tracking) bizim sinyallerimizden biri olacak — bu zaten bir endüstri haline geldi ("Kolscan of Polymarket" diye tabir ediliyor). Nisan 2024 - Nisan 2025 arasında ileri trader'lar bu tür stratejilerle ~$40M çekti.

Sources: [Polymarket Leaderboard](https://polymarket.com/leaderboard/overall/monthly/profit), [Polycopy - 30 Best Polymarket Traders](https://polycopy.app/best-polymarket-traders), [Webopedia - Top Polymarket Traders](https://www.webopedia.com/crypto/learn/top-polymarket-traders/), [Datawallet - Top Polymarket Strategies](https://www.datawallet.com/crypto/top-polymarket-trading-strategies)

---

## 5. Edge'in beş kaynağı (Poly Meridian'ın sömüreceği)

Bu beş kaynak agent'ımızın **stratejik sütunları** olacak. Hibrit mimari her birini paralel çalıştıracak.

### 5.1 Statistical/Combinatorial Arbitrage
İki tip:
- **Tek-market:** YES + NO ≠ $1.00 olunca arbitraj.
- **Cross-market:** Mantıksal olarak ilişkili marketler arasında tutarsızlık (örn. "X parti kazanır" + "Y aday kazanır" topla $1.00'ı geçemez).
- **Cross-platform:** Aynı olay Polymarket ile Kalshi arasında farklı fiyatlanıyor.

**Beklenen getiri:** %0.5 – %3 per opportunity, dakikalar içinde kapanır. **Yıllık Sharpe potansiyeli yüksek**, ama hacim sınırlı.

### 5.2 News & Sentiment Arbitrage
Breaking news, oran hareketinden **önce** geliyor. Twitter/X postu, NYT manşeti, Truth Social pattern'ı, Fed açıklaması — bunlar saniyeler içinde fiyatı 5-10 puan oynatabilir.

**Yaklaşım:** LLM tabanlı sentiment scoring. Bir haber çıktığı an, biz şunları yapıyoruz:
1. Hangi market(ler)i etkiliyor — semantic matching.
2. Yön: pozitif/negatif.
3. Şiddet: 0–1 ölçeği (model tahmini).
4. Order book'taki mevcut fiyatla karşılaştır, fırsat varsa pozisyon aç.

Akademik araştırmalar gösteriyor ki finansal sentiment için **FinBERT/DeBERTa ensemble** modelleri %80'e kadar doğruluğa ulaşıyor.

Sources: [arXiv - LLM-Enhanced Tweet Emotion Analysis](https://arxiv.org/html/2510.03633v1), [Springer - LLM News Sentiment Predictor](https://link.springer.com/article/10.1007/s10791-025-09573-7)

### 5.3 Smart Money Tracking
Polygon explorer üzerinden top trader cüzdanlarını izlemek. Bir cüzdan, geçmiş ROI %200+ ve örneklem >100 trade ise "smart money" olarak işaretle.

**Sinyal:** Smart money cluster aynı yöne hareket etmeye başladığında biz de düşük gecikmeyle takip ediyoruz. Bu, copy trading'in algoritmik versiyonudur.

**Risk:** Smart money de kaybedebilir; ve big wallet'lar bazen aşırı koreledir. Bu yüzden sadece çok yüksek conviction trade'lerde tetikler.

### 5.4 Quant Modeller (Polymarket'in kendi tarihsel verisi)
- **Mean reversion:** Bir market kısa sürede aşırı oynaklığa girdiyse, geri çekilme bekle.
- **Momentum:** Resolution'a yaklaşırken fiyat hareketi hızlanır — trend süreklilik gösterir.
- **Volatility regimes:** Marketleri yüksek/orta/düşük vol rejimlerine ayır, her birinde farklı strateji.
- **Time decay:** Resolution yaklaştıkça belirsizlik azalır — implied prob ile gerçek prob arasındaki fark belli pattern'lar gösterir.

### 5.5 Domain-Specific Fundamentals
Belirli kategorilerde **structured data** ile model kurmak:
- **Politics:** Poll aggregator (538-style), poll bias correction, kampanya finansmanı.
- **Sports:** Elo ratings, injury reports, weather.
- **Crypto:** On-chain metrics (exchange flows, funding rates), TA göstergeleri.
- **Macro:** Fed dot plot, ekonomik takvim.

**Edge:** Bunları **otomatik** ve **paralel** yapan kimse yok. Manuel uzmanlar tek kategoride iyi, biz **hepsinde "iyi-ye-yakın"** olabiliriz — quant'ın tarihsel avantajı.

---

## 6. Quant tarihi: Renaissance/Medallion mantığı bize ne öğretir

Jim Simons ve Renaissance Technologies'in Medallion Fonu, 1988–2018 arası **yıllık %66 brüt** (yönetim ücreti sonrası ~%39 net) getiri yaptı. Bu insanlık tarihinin en iyi performansı. Onları neyin böyle yaptığını anlamak Poly Meridian için anayasamızdır:

### Onlardan alacağımız 7 ders

1. **Çok sayıda zayıf sinyal > az sayıda güçlü sinyal.**
   Medallion'da herhangi tek bir sinyal %50.5 - %52 doğru. Ama yüzlerce uncorrelated sinyalin toplamı = istikrarlı edge. Bize ders: tek bir "süper strateji" arama. 30 farklı zayıf sinyali topla.

2. **Veriye dayan, fikre dayanma.**
   Trader'ın gut feeling'i yok. Sadece backtested, p<0.01 anlamlı sinyaller. Bize ders: her sinyalin **istatistiksel testi** olacak.

3. **Kısa tutma süreleri.**
   Medallion'un ortalama tutma süresi günler/saatler. Biz: marketlerin doğası gereği günler-haftalar, ama içinde de hızlı rebalancing.

4. **İşlem maliyetlerinin obsesif takibi.**
   Renaissance tarihinin en pahalı (insan-saati) yatırımı: execution algoritması. Bize ders: maker olmak, slippage modeli, smart order routing.

5. **Risk = aşılmaz duvar.**
   Renaissance'ın kuralları: bir gün max kayıp %5. Total leverage'da sert tavanlar. Bize ders: **Kill-switch zorunlu, drawdown limit zorunlu, asla şahsi yargıyla bypass edilmemeli.**

6. **Sürekli yenilenme.**
   Bir sinyal "kalabalıklaşırsa" decay eder. Renaissance ekibi sürekli yeni sinyal arar. Bize ders: agent **kendi kendini değerlendirsin**, decay olan stratejileri kapatıp yenisini açsın.

7. **Kapasite sınırı = gerçek.**
   Medallion AUM'unu kasten kapalı tuttu (~$10B). Çünkü her stratejinin bir kapasitesi var. Bize ders: Polymarket'in marketlerinin de likidite sınırı var — büyük pozisyon = market impact = edge erozyon. **Pozisyon büyüklüğü piyasanın günlük hacminin belli bir oranını aşmamalı.**

Sources: [Renaissance Technologies & Medallion - Quartr](https://quartr.com/insights/edge/renaissance-technologies-and-the-medallion-fund), [Jim Simons Trading Strategy - QuantVPS](https://www.quantvps.com/blog/jim-simons-trading-strategy), [How Renaissance Built $100B - Medium](https://medium.com/@navnoorbawa/how-renaissance-technologies-aqr-and-pdt-built-100-billion-factor-models-statistical-arbitrage-ac0c9cd8a518)

---

## 7. Gerçekçi getiri beklentileri ve fon ekonomisi

Şimdi bir girişimci/fon yöneticisi gibi düşünelim. Para konuşalım.

### 7.1 Yıllık getiri senaryoları

Polymarket retail trader ortalamasının **uzun vadede negatif** olduğunu varsayalım (her hisse senedi piyasasındaki gibi — taker fee + bilgi asimetrisi nedeniyle). Algoritmik bir agent için gerçekçi hedefler:

| Senaryo       | Yıllık net getiri | Max drawdown | Sharpe | Açıklama |
|---------------|-------------------|--------------|--------|----------|
| Pesimist      | %15–25            | %20          | 0.8    | Sadece arbitraj edge'i + maker rebate |
| Baz (realist) | %40–80            | %25          | 1.5–2.0| Hibrit, +sentiment, +smart money |
| İyimser       | %150–300          | %30          | 2.5+   | Tüm edge'ler aktif, küçük AUM avantajı |
| Medallion-vari | %66 net (≈ %100+ brüt) — referans, hedef değil |

**Kritik gerçek:** Edge AUM ile ters orantılı. $50K ile %200 yapan strateji $5M'da %50, $50M'da %15 yapar. Polymarket'in kapasitesi şu an *bir kişisel/küçük fonu rahatlıkla taşır*, ama $100M+ olursa marketleri kendin hareket ettirirsin = edge biter.

### 7.2 Fon ekonomisi modeli (kendi fonumuz için)

**Maliyetler (yıllık):**
- Sunucu & altyapı: $3K–$8K (VPS + DB + monitoring)
- API'ler ve veri: $2K–$15K (news API, social, vs.)
- LLM inference (sentiment): $5K–$30K (kullanıma göre)
- Geliştirme zamanı (sermaye değil, fırsat maliyeti): değerlendirin
- Toplam baz operasyon: ~$15K–$50K/yıl

**Getiri sermayesinde örnek:**
- Sermaye: $250K
- Hedef baz getiri: %60 → $150K/yıl
- Op gider: $30K
- **Net: $120K/yıl, ROE %48**

**Önemli hesap:** Polymarket marketleri çoğu zaman *birkaç ay* sürüyor — yani sermayen sürekli kilitli. **Cash drag** problemi var. İyi rebalancing ve **sermaye verimliliği KPI**'sı (capital turnover) kritik.

### 7.3 Kelly Criterion ile pozisyon büyüklüğü

Bu Poly Meridian'ın **matematiksel kalbidir.** Kelly formülü, oranı bildiğin bir bahiste sermayenin ne kadarını yatırman gerektiğini söyler:

```
f* = (b·p − q) / b
```

Burada:
- **f*** = sermayenin yatırılacak fraksiyonu
- **b** = net odds (örn. $0.40'a alıp $1.00'a satıyorsan b = 1.5)
- **p** = bizim modelimizin verdiği kazanma olasılığı
- **q** = 1 − p

**Örnek:** YES token $0.40'tan satılıyor. Biz %70 olasılık görüyoruz.
- b = (1.00 − 0.40) / 0.40 = 1.5
- p = 0.70, q = 0.30
- f* = (1.5 × 0.70 − 0.30) / 1.5 = 0.50

Yani Kelly sermayenin **%50'sini** koymanı söyler. Bu çok agresif — çünkü Kelly mükemmel olasılık tahmini varsayar. Bizim modelimiz mükemmel değil.

**Pratik kural — Fractional Kelly:**
- Full Kelly → %33 ihtimalle bankroll'unu yarıya iniyor (Renaissance asla full Kelly kullanmaz)
- **Half Kelly** → büyümenin %75'i, drawdown'un yarısı (endüstri standardı)
- **Quarter Kelly** → çok daha güvenli, hâlâ yeterli büyüme

**Poly Meridian default:** **Quarter Kelly** kullanacağız ve **tek bir pozisyon asla bankroll'un %5'ini geçmeyecek** (hard cap).

Sources: [Kelly Criterion - Prediction Hunt](https://www.predictionhunt.com/blog/prediction-market-position-sizing-kelly-criterion), [Kelly Criterion - Wikipedia](https://en.wikipedia.org/wiki/Kelly_criterion), [Astute Investors - Kelly](https://astuteinvestorscalculus.com/the-kelly-criterion/)

### 7.4 Sharpe Ratio ve risk-ayarlı performans

Tek başına getiri yetmez. Bir fonu ölçmenin doğru yolu **Sharpe Ratio**:

```
Sharpe = (Yıllık Getiri − Risksiz Faiz) / Yıllık Volatilite
```

Sektör referansları:
- Pasif S&P 500: Sharpe ~0.5
- İyi hedge fonu: Sharpe 1.0–1.5
- Renaissance Medallion: Sharpe >2.5
- Bizim hedefimiz: **Sharpe 1.5+ (baz), 2.0+ (iyimser)**

---

## 8. Hukuki çerçeve, vergi, risk haritası

> **Uyarı:** Bu bölüm bilgilendirme amaçlıdır, hukuki/finansal tavsiye değildir. Türkiye ve uluslararası boyutta avukat/mali müşavirle çalışın.

### 8.1 Türkiye perspektifi
- Polymarket Türkiye'den IP kısıtlamasıyla teknik olarak doğrudan erişilebilir değil; kullanıcılar VPN veya farklı yetki alanlarındaki şirket yapıları üzerinden erişiyor.
- USDC işlemleri kripto regülasyonu kapsamında — SPK ve MASAK uyum gereksinimleri var.
- Vergisel açıdan: prediction market kazançları Türk vergi mevzuatında net olarak tanımlı değil. Yatırım geliri / kumar geliri arasında bir gri alan. **Mali müşavirle danışın** — büyük olasılıkla "diğer kazanç ve iratlar" altında beyan edilebilir.

### 8.2 ABD durumu (2026)
- Kasım 2025'te Polymarket **CFTC Amended Order of Designation** aldı. Aralık 2025'te US'de yeniden açıldı.
- Federal düzeyde yasal. Eyalet bazında (Tennessee, Massachusetts, Nevada) gri/kısıtlı.
- KYC zorunlu, broker üzerinden trade.
- IRS henüz prediction market kazançlarını net sınıflandırmadı — kullanıcı self-report yapıyor.

### 8.3 Risk haritası

| Risk           | Açıklama | Mitigasyon |
|----------------|----------|------------|
| Regülasyon     | Platform birden kapanabilir / coğrafi kısıt | Coğrafi diversifikasyon, Kalshi'yi de izle, hızlı çekim planı |
| Smart contract | Polygon kontratı exploit | Çoklu cüzdan, sermaye çoğunluğunu cold storage'da tut |
| Likidite şoku  | Marketin aniden derinliğini kaybetmesi | Pozisyon büyüklüğü %5 hard cap, likidite filtreli market seçimi |
| Resolution disputes | Bir market "ambiguous" olarak işaretlenir | Resolution mekaniklerini iyi anla, UMA oracle bağımlılığı |
| Model decay    | Stratejilerimiz market tarafından "öğrenilir" | Sürekli yeni feature ekleme, dönemsel re-train |
| Operasyonel    | Sunucu/agent bug, yanlış emir | Paper-trade önce, kill-switch, anomaly detection |
| Latency        | Haber-bazlı sinyallerde gecikme = kayıp | Kendi sunucumuz, optimize edilmiş websocket, redundant feed'ler |

Sources: [Polymarket US Legal Status - Alphascope](https://www.alphascope.app/blog/is-polymarket-legal-in-us), [Polymarket KYC Process](https://www.tradetheoutcome.com/how-the-kyc-process-works-on-polymarket-us-and-required-documents/), [US Prediction Market State-by-State - Lines.com](https://www.lines.com/guides/u-s-prediction-market-legal-status-state-by-state)

---

---

# BÖLÜM 2 — POLY MERIDIAN MİMARİSİ (Claude Code için spec)

> Bu bölüm Claude Code'a verilecek teknik şartnamedir. Türkçe açıklamalar, İngilizce kod isimleri. Claude Code bu dökümanı baştan sona okuduğunda repoyu sıfırdan inşa edebilmelidir.

## 9. Sistem genel bakış

```
┌─────────────────────────────────────────────────────────────────────┐
│                         POLY MERIDIAN AGENT                         │
└─────────────────────────────────────────────────────────────────────┘

  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
  │   Polymarket│   │  News APIs  │   │  Twitter/X  │   │   On-Chain  │
  │  Gamma+CLOB │   │ (GDELT/etc) │   │   (X API)   │   │  (Polygon)  │
  └──────┬──────┘   └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
         │                 │                 │                 │
         ▼                 ▼                 ▼                 ▼
  ╔══════════════════════════════════════════════════════════════════╗
  ║                      DATA INGESTION LAYER                        ║
  ║  · WebSocket consumers · REST pollers · Smart wallet tracker     ║
  ╚══════════════════════════════════════════════════════════════════╝
                                  │
                                  ▼
  ╔══════════════════════════════════════════════════════════════════╗
  ║         STORAGE — PostgreSQL + TimescaleDB + Redis (cache)       ║
  ║  · markets · orderbook_snapshots · trades · news · features      ║
  ╚══════════════════════════════════════════════════════════════════╝
                                  │
                                  ▼
  ╔══════════════════════════════════════════════════════════════════╗
  ║                  FEATURE ENGINEERING LAYER                       ║
  ║  · TA features · sentiment scores · smart-money flow · time-decay║
  ╚══════════════════════════════════════════════════════════════════╝
                                  │
                                  ▼
  ╔══════════════════════════════════════════════════════════════════╗
  ║                      STRATEGY LAYER (5 alt-strateji)             ║
  ║  arbitrage · sentiment · smart_money · stat_quant · fundamentals ║
  ║                          ↓ her biri sinyal üretir                ║
  ╚══════════════════════════════════════════════════════════════════╝
                                  │
                                  ▼
  ╔══════════════════════════════════════════════════════════════════╗
  ║              SIGNAL AGGREGATOR + RISK ENGINE                     ║
  ║  · Kelly sizing · exposure caps · kill-switch · daily loss limit ║
  ╚══════════════════════════════════════════════════════════════════╝
                                  │
                                  ▼
  ╔══════════════════════════════════════════════════════════════════╗
  ║                   EXECUTION ENGINE (CLOB)                        ║
  ║  · order routing · maker-first · cancel/replace · slippage guard ║
  ╚══════════════════════════════════════════════════════════════════╝
                                  │
                                  ▼
  ╔══════════════════════════════════════════════════════════════════╗
  ║                    PORTFOLIO MANAGER + P&L                       ║
  ║  · real-time MTM · greeks · realized vs unrealized               ║
  ╚══════════════════════════════════════════════════════════════════╝
                                  │
                                  ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │   OBSERVABILITY — Prometheus + Grafana + Slack/Telegram alerts  │
  └─────────────────────────────────────────────────────────────────┘
```

**Operasyon modları:**
- `paper` (default başlangıç): Gerçek emir gönderilmez, simüle edilir.
- `live-conservative`: Gerçek emir, ama tek pozisyon max $50, gün max $200.
- `live-normal`: Tam sermaye.
- `kill`: Tüm pozisyonları kapat, agent durdur.

---

## 10. Repo yapısı ve dosya envanteri

```
poly-meridian/
├── README.md
├── pyproject.toml                 # poetry/uv config
├── docker-compose.yml
├── docker-compose.prod.yml
├── Dockerfile
├── .env.example
├── .gitignore
├── Makefile                       # make up / make test / make backtest
│
├── config/
│   ├── base.yaml                  # tüm default'lar
│   ├── paper.yaml                 # paper mode override
│   ├── live.yaml                  # live mode override
│   ├── strategies/
│   │   ├── arbitrage.yaml
│   │   ├── sentiment.yaml
│   │   ├── smart_money.yaml
│   │   ├── stat_quant.yaml
│   │   └── fundamentals.yaml
│   └── risk.yaml                  # Kelly fraction, caps, limits
│
├── src/poly_meridian/
│   ├── __init__.py
│   ├── main.py                    # entrypoint, asyncio event loop
│   ├── settings.py                # pydantic-settings, env vars
│   │
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── gamma_client.py        # Polymarket Gamma REST (markets metadata)
│   │   ├── clob_client.py         # Polymarket CLOB (trading + book)
│   │   ├── clob_ws.py             # WebSocket consumer
│   │   ├── news_provider.py       # GDELT + fallback'ler
│   │   ├── twitter_provider.py    # X API v2 streaming
│   │   ├── onchain_provider.py    # Polygon RPC, smart wallet tracker
│   │   └── normalize.py           # tüm kaynakları unified event'e döker
│   │
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── db.py                  # asyncpg + connection pool
│   │   ├── models.py              # SQLAlchemy ORM
│   │   ├── migrations/            # alembic
│   │   └── cache.py               # Redis wrapper
│   │
│   ├── features/
│   │   ├── __init__.py
│   │   ├── orderbook_features.py  # bid-ask spread, depth imbalance, microprice
│   │   ├── time_features.py       # time-to-resolution, decay
│   │   ├── sentiment_features.py  # LLM-based scoring
│   │   ├── smart_money_features.py# top trader cluster flow
│   │   ├── ta_features.py         # rolling vol, RSI-like on prices
│   │   └── registry.py            # feature catalog
│   │
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── base.py                # Strategy ABC
│   │   ├── arbitrage.py
│   │   ├── sentiment.py
│   │   ├── smart_money.py
│   │   ├── stat_quant.py
│   │   ├── fundamentals.py
│   │   └── aggregator.py          # her stratejinin sinyalini birleştirir
│   │
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── kelly.py               # fractional Kelly
│   │   ├── limits.py              # exposure, daily loss, concentration
│   │   ├── kill_switch.py
│   │   └── policy.py              # her trade'i RiskPolicy'den geçirir
│   │
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── order_router.py        # maker-first logic
│   │   ├── order_book.py          # local book replica
│   │   ├── slippage_model.py
│   │   ├── paper_executor.py      # simüle edilmiş fill
│   │   └── live_executor.py       # gerçek CLOB submit
│   │
│   ├── portfolio/
│   │   ├── __init__.py
│   │   ├── ledger.py              # double-entry book
│   │   ├── mark_to_market.py
│   │   ├── pnl.py                 # realized + unrealized
│   │   └── rebalancer.py
│   │
│   ├── backtest/
│   │   ├── __init__.py
│   │   ├── replay.py              # historical replay engine
│   │   ├── walkforward.py
│   │   ├── metrics.py             # Sharpe, Sortino, max DD, win rate
│   │   └── reports.py             # markdown/html report
│   │
│   ├── observability/
│   │   ├── __init__.py
│   │   ├── logging_config.py      # structlog
│   │   ├── metrics.py             # prometheus_client
│   │   ├── tracing.py             # opentelemetry (optional)
│   │   └── alerts.py              # slack/telegram webhooks
│   │
│   └── cli.py                     # typer / click — run, backtest, status
│
├── notebooks/                     # research / ad-hoc analiz
│   ├── 01_explore_markets.ipynb
│   ├── 02_sentiment_eval.ipynb
│   └── 03_strategy_research.ipynb
│
├── scripts/
│   ├── bootstrap_db.sh
│   ├── backfill_history.py
│   └── promote_to_live.py         # paper→live geçiş gating check
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── strategy/                  # her stratejinin synthetic backtest'i
│
└── docs/
    ├── architecture.md
    ├── runbook.md                 # operational procedures
    ├── strategy_specs/
    └── api_notes.md
```

---

## 11. Veri katmanı (Data Ingestion)

### 11.1 Polymarket Gamma API
**Base URL:** `https://gamma-api.polymarket.com`
**Auth:** Yok (public, read-only)
**Rate limit:** 15,000 istek / 10 saniye

**Önemli endpoint'ler:**
- `GET /markets?active=true&closed=false&limit=500` — açık marketleri listele
- `GET /markets/{condition_id}` — tek market detayı
- `GET /events?active=true` — event grupları (negatif risk marketler için kritik)

**Polling stratejisi:**
- Tüm aktif marketlerin listesi: 5 dakikada bir
- Yeni marketlerin keşfi: 1 dakikada bir
- Event/condition mapping: günde bir kez tam senkron

### 11.2 Polymarket CLOB API
**Base URL:** `https://clob.polymarket.com`
**Auth:** L1 (EIP-712 wallet sig) → L2 (HMAC-SHA256)

**Akış:**
```python
# L1 ile API key türet (bir defa)
client = ClobClient(host=CLOB_HOST, chain_id=137, key=PRIVATE_KEY)
creds = client.create_or_derive_api_key()

# L2 ile kalıcı authenticated client
client = ClobClient(host=CLOB_HOST, chain_id=137, key=PRIVATE_KEY, creds=creds)

# Order types
client.create_order(OrderArgs(price=0.42, size=100, side=BUY, token_id=...), order_type="GTC")
client.create_market_order(MarketOrderArgs(amount=50, token_id=..., side=BUY))  # FOK/FAK
```

**Library:** `py-clob-client-v2` (yeni) veya `py-clob-client` (klasik). v2 önerilir.

Sources: [Polymarket Authentication](https://docs.polymarket.com/api-reference/authentication), [py-clob-client-v2 GitHub](https://github.com/Polymarket/py-clob-client-v2), [Polymarket API Tutorial - AgentBets](https://agentbets.ai/guides/polymarket-api-guide/)

### 11.3 WebSocket (gerçek zamanlı)
**Market channel URL:** `wss://ws-subscriptions-clob.polymarket.com/ws/market`
**Auth:** Yok (public)
**Subscribe:**
```json
{
  "auth": {},
  "type": "market",
  "assets_ids": ["<token_id_1>", "<token_id_2>", ...]
}
```
**Mesaj tipleri:** `book`, `price_change`, `tick_size_change`, `last_trade_price`
**Heartbeat:** Her 10 saniyede bir PING gönder
**Reconnect:** Disconnect olunca REST snapshot çekip yeniden subscribe ol.

**Local order book reconstruction:**
- `book` mesajı = tam snapshot
- `price_change` = incremental update
- Quantity 0 olan price level'ı kaldır

Sources: [Polymarket WebSocket Overview](https://docs.polymarket.com/market-data/websocket/overview), [Polymarket WebSocket Guide](https://agentbets.ai/guides/polymarket-websocket-guide/)

### 11.4 Haber verisi
**Ana sağlayıcı:** **GDELT** (ücretsiz, 100+ ülke, 15 dakikada bir güncellenir, BigQuery üzerinden tarihsel)
**Yedek/zenginleştirici:**
- NewsAPI ($449/ay) — gerekirse premium
- Benzinga Basic API (ücretsiz tier) — finansal haberler için
- RSS feed kümeleri (NYT, WaPo, Reuters, FT, Bloomberg headlines)

**İşleme:**
1. Her makaleyi normalize et: `{title, body, source, timestamp, url}`
2. Polymarket market'leriyle semantic match (embedding cosine similarity)
3. LLM ile sentiment + impact score
4. `news_signals` tablosuna yaz

Sources: [GDELT - Best News API Alternative](https://dataresearchtools.com/gdelt-project-for-news-data-2026-free-alternative-to-newsapi/), [News API Pricing Comparison](https://dataresearchtools.com/best-news-apis-comparison/)

### 11.5 Twitter/X
**X API v2** (paid tier — ucuz başlangıç: Basic $200/ay)
- Filtered stream rule'ları: tracked political figures, financial accounts, key journalists
- Her tweet'i sentiment scoring'e gönder
- Verified ve >100K follower'lı hesaplara extra ağırlık

### 11.6 On-chain (Polygon)
**RPC:** Alchemy / Infura / kendi node'umuz
**Hedef:**
- Polymarket Conditional Tokens kontratı: tüm trade event'leri
- Top trader cüzdanlarının real-time aktivitesi
- USDC transferleri (likidite akışı sinyali)

**Tooling:** `web3.py`, opsiyonel `subgraph` (The Graph) sorguları.

### 11.7 Leaderboard provider (v1.1)
**Amaç:** Polymarket'in kendi leaderboard'undan top trader listesi + metadata çekmek, `smart_wallets` tablosunu güncel tutmak.

**Sources (denendiği sırayla):**
1. `https://data-api.polymarket.com/leaderboard` veya benzeri public data endpoint (önerilen yol)
2. Network tab inspection ile keşfedilen private API endpoint
3. HTML fallback: `https://polymarket.com/leaderboard/{category}/{period}/{sort}` — React SPA olduğu için Playwright ile render

**Polling:** Günde 1x cron job (yeterli; leaderboard saatte hareket etmiyor).

**Metadata çekilen alanlar:**
- Cüzdan adresi (0x...)
- Display name (varsa)
- Lifetime PnL, win rate, trade count
- Son 7d PnL, son 7d drawdown
- Aktivite freshness (son trade ne zaman)
- Kategori odağı (lifetime hacminin %X'i hangi kategoride)

**Tier ataması (cron task içinde):**
- Tier 1 koşulları sağlanıyorsa → tier=1
- Tier 2 koşulları → tier=2
- Aksi halde tier=3

**Tooling:** `httpx` async client, opsiyonel `playwright` HTML fallback için.

**Risk uyarısı:** Leaderboard rakamları **survivorship bias** içerir. Sadece kazananlar görünür; aynı kişi gelecekte iflas edebilir. Bu yüzden tier ataması "gözlem altında" (Tier 3) varsayılan davranıştır — promotion ancak 90+ gün tutarlı performansla olur.

---

## 12. Veritabanı şeması

**Stack:** PostgreSQL 16 + TimescaleDB (zaman serisi için).

### Tablolar

```sql
-- Markets metadata (Gamma'dan)
CREATE TABLE markets (
    condition_id    TEXT PRIMARY KEY,
    question        TEXT NOT NULL,
    category        TEXT,
    sub_category    TEXT,
    event_id        TEXT,
    yes_token_id    TEXT NOT NULL,
    no_token_id     TEXT NOT NULL,
    end_date_iso    TIMESTAMPTZ,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    closed          BOOLEAN NOT NULL DEFAULT FALSE,
    liquidity_num   NUMERIC,
    volume_num      NUMERIC,
    raw             JSONB,                              -- ham gamma response
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_markets_active ON markets(active, closed, end_date_iso);
CREATE INDEX idx_markets_event ON markets(event_id);

-- Order book snapshots (TimescaleDB hypertable)
CREATE TABLE orderbook_snapshots (
    ts              TIMESTAMPTZ NOT NULL,
    token_id        TEXT NOT NULL,
    best_bid        NUMERIC,
    best_ask        NUMERIC,
    mid             NUMERIC,
    microprice      NUMERIC,
    bid_depth_5pct  NUMERIC,
    ask_depth_5pct  NUMERIC,
    raw_levels      JSONB
);
SELECT create_hypertable('orderbook_snapshots', 'ts');
CREATE INDEX idx_obs_token_ts ON orderbook_snapshots(token_id, ts DESC);

-- Trades (hem bizim hem on-chain'den genel)
CREATE TABLE trades (
    ts              TIMESTAMPTZ NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    price           NUMERIC NOT NULL,
    size            NUMERIC NOT NULL,
    maker_address   TEXT,
    taker_address   TEXT,
    tx_hash         TEXT,
    is_ours         BOOLEAN NOT NULL DEFAULT FALSE
);
SELECT create_hypertable('trades', 'ts');
CREATE INDEX idx_trades_token ON trades(token_id, ts DESC);
CREATE INDEX idx_trades_addr ON trades(maker_address, taker_address);

-- News articles
CREATE TABLE news_articles (
    article_id      TEXT PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    source          TEXT,
    title           TEXT,
    body            TEXT,
    url             TEXT,
    embedding       VECTOR(768),                        -- pgvector
    processed       BOOLEAN NOT NULL DEFAULT FALSE
);

-- News → market sinyali
CREATE TABLE news_signals (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    article_id      TEXT NOT NULL REFERENCES news_articles(article_id),
    condition_id    TEXT NOT NULL REFERENCES markets(condition_id),
    sentiment       NUMERIC NOT NULL,                   -- -1..1
    impact          NUMERIC NOT NULL,                   -- 0..1
    direction       TEXT NOT NULL CHECK (direction IN ('YES','NO','NEUTRAL'))
);

-- Smart money cüzdanları (v1.1: 3-tier + recency_score + hedge_flag)
CREATE TABLE smart_wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT,
    lifetime_pnl    NUMERIC,
    win_rate        NUMERIC,
    trade_count     INT,
    last_updated    TIMESTAMPTZ,
    -- v1.1 columns
    tier            INT NOT NULL DEFAULT 3 CHECK (tier IN (1, 2, 3)),
    category_focus  TEXT,                       -- 'Politics' | 'Crypto' | 'Sports' | 'Mixed' | NULL
    last_7d_pnl     NUMERIC,
    recency_score   NUMERIC DEFAULT 0,          -- 0..1 — son aktivite freshness'ı
    hedge_flag      BOOLEAN NOT NULL DEFAULT FALSE,  -- biliniyorsa: cüzdan hedge-trader (raw signal'ı düşür)
    drawdown_7d_pct NUMERIC                     -- son 7 günlük drawdown, loss filter için
);

-- Feature snapshots (her tickte hesaplanan, ML feature seti)
CREATE TABLE feature_snapshots (
    ts              TIMESTAMPTZ NOT NULL,
    token_id        TEXT NOT NULL,
    features        JSONB NOT NULL                      -- key-value feature map
);
SELECT create_hypertable('feature_snapshots', 'ts');

-- Strategy sinyalleri (her stratejinin çıktısı)
CREATE TABLE strategy_signals (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    strategy        TEXT NOT NULL,
    condition_id    TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    edge            NUMERIC NOT NULL,                   -- bizim_p - market_p
    conviction      NUMERIC NOT NULL,                   -- 0..1
    suggested_action TEXT NOT NULL CHECK (suggested_action IN ('BUY_YES','BUY_NO','SELL','HOLD','EXIT')),
    rationale       JSONB
);

-- Bizim orderlarımız ve fillerimiz
CREATE TABLE our_orders (
    order_id        TEXT PRIMARY KEY,
    ts_created      TIMESTAMPTZ NOT NULL,
    ts_filled       TIMESTAMPTZ,
    strategy        TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    price           NUMERIC,
    size            NUMERIC,
    filled_size     NUMERIC NOT NULL DEFAULT 0,
    avg_fill_price  NUMERIC,
    status          TEXT NOT NULL,                      -- PENDING/LIVE/PARTIAL/FILLED/CANCELLED
    mode            TEXT NOT NULL                       -- 'paper' veya 'live'
);

-- Portfolio (anlık pozisyon görünümü)
CREATE TABLE positions (
    token_id        TEXT PRIMARY KEY,
    qty             NUMERIC NOT NULL,
    avg_cost        NUMERIC NOT NULL,
    last_mark       NUMERIC NOT NULL,
    last_updated    TIMESTAMPTZ NOT NULL
);

-- Günlük P&L
CREATE TABLE pnl_daily (
    date            DATE PRIMARY KEY,
    starting_nav    NUMERIC NOT NULL,
    ending_nav      NUMERIC NOT NULL,
    realized        NUMERIC NOT NULL,
    unrealized      NUMERIC NOT NULL,
    fees            NUMERIC NOT NULL,
    trade_count     INT NOT NULL,
    win_count       INT NOT NULL
);
```

---

## 13. Feature engineering ve sinyal katmanı

Her order book ticking'inde (örn. her 5 sn'de bir), her takip edilen token için aşağıdaki feature seti hesaplanır ve `feature_snapshots`'a yazılır:

**Order book features:**
- `mid_price` = (best_bid + best_ask) / 2
- `spread` = best_ask − best_bid
- `microprice` = (best_bid × ask_size + best_ask × bid_size) / (bid_size + ask_size)
- `depth_imbalance_5pct` = (bid_depth_5pct − ask_depth_5pct) / (bid_depth_5pct + ask_depth_5pct)

**Time features:**
- `time_to_resolution_hours`
- `log_time_to_resolution`

**Volatility features (rolling):**
- `vol_1h`, `vol_24h` (price std)
- `volume_1h`, `volume_24h`
- `trade_count_1h`

**Sentiment features (LLM tarafından hesaplanır):**
- `news_sentiment_1h_avg`
- `news_impact_1h_max`
- `twitter_sentiment_1h`
- `twitter_volume_1h` (mention sayısı)

**Smart money features:**
- `smart_money_net_buy_1h` (smart wallet buyflow − sellflow)
- `smart_money_cluster_signal` (kaç farklı smart wallet aynı yöne)

**Cross-market features:**
- `arb_imbalance` = YES_mid + NO_mid − 1.00
- `related_market_drift` (event grubundaki diğer marketler hareket etti mi)

Feature registry (`features/registry.py`) her feature için: isim, hesaplama fn, gerekli pencere, NaN politikası, dependency tutar.

---

## 14. Strateji katmanı (5 alt-strateji)

Her strateji `BaseStrategy` ABC'sini implement eder:

```python
class BaseStrategy(ABC):
    name: str
    enabled: bool
    config: dict

    @abstractmethod
    async def evaluate(self, market: Market, features: dict) -> Optional[StrategySignal]:
        """Returns signal or None."""

    @abstractmethod
    def capacity_estimate(self) -> float:
        """USD cinsinden bu strateji için tahmini günlük kapasite."""
```

### 14.1 ArbitrageStrategy
**Tetik:**
- Tek market: `|YES_ask + NO_ask − 1.00| > threshold` (default 0.015)
- Event seti: tüm YES'lerin toplamı $1.00'dan saparsa
- Cross-platform: Kalshi entegrasyonu eklendikten sonra

**Aksiyon:** Risk-free complete-set arbitrajı; her zaman maker-first (limit order ile).

**Convictions:** 0.95 (matematiksel olarak guaranteed iken; execution risk hariç).

### 14.2 SentimentStrategy
**Girdi:** Son 30 dakikadaki `news_signals` + `twitter_sentiment` ortalaması.
**Edge:** Sentiment yönü ile market fiyat yönü arasındaki kopukluk.
**Filtre:** Sadece `impact > 0.6` olan sinyalleri dikkate al.
**Conviction:** `impact × |sentiment|` (0..1)

**Aksiyon:** Yönlü pozisyon (BUY_YES veya BUY_NO).

### 14.3 SmartMoneyStrategy — 3-tier (v1.1)
**Tetik mantığı 3 tier'a bölündü.** Survivorship bias + adverse selection + reflexivity riskleri için her tier ayrı eşik + ayrı pozisyon ağırlığı.

#### Tier 1 — Kanıtlanmış (auto-trade, full weight)
- Lifetime PnL > $500K
- Win rate > %55
- Trade count > 200
- Aktif >90 gün
- Son 7 günde -%20 drawdown YOK
- Cluster: ≥3 farklı Tier 1 cüzdan aynı yöne, her biri ≥$5K net buy, son 30dk
- Position weight: Kelly × 1.0 (normal)

#### Tier 2 — Sıcak ama tedbirli (auto-trade, half weight)
- Son 30 gün PnL > +$50K
- Son 30 gün win rate > %52
- Cluster: ≥2 Tier 2 cüzdan aynı yöne, son 30dk
- Position weight: Kelly × 0.5

#### Tier 3 — Yeni keşif (DASHBOARD ONLY, default no auto-trade)
- Son 7 günde leaderboard'a ilk girenler (yüksek volatility, gözlem altında)
- Surfacing yapılır, operatör manuel onay verirse pozisyon açılır
- Position weight: Kelly × 0 (default), config ile aktive edilirse × 0.25

#### Zorunlu filtreler (tüm tier'lar için)
- **Latency decay:** Cüzdan işlemini 30dk+ sonra görüyorsak pas (fiyat zaten kaçtı)
- **Cluster confirmation:** Tek whale takibi YASAK
- **Position size cap:** Copy-trade max bankroll %2'si (regular Kelly'nin yarısı, §15.1)
- **Per-trader concentration cap:** Tek trader'dan max %5 portfolio exposure
- **Hedge kontrolü:** İlişkili marketlerde ters pozisyon var mı? Varsa sinyal düşürülür
- **Loss filter:** Cüzdan son 7 günde -%20 drawdown'daysa dışla
- **Attribution log:** Her copy-trade `Order.rationale` içinde `copied_from={tier}_{wallet}` notu

#### Veri kaynakları (çift-feed redundancy)
- **Primary:** `ingestion/onchain_provider.py` — Polygon RPC, real-time CTF transfer events
- **Secondary:** `ingestion/leaderboard_provider.py` — Polymarket leaderboard API/HTML polling, günde 1x cüzdan listesi + metadata güncellemesi
- **Cluster builder:** Background task, on-chain queue'sundan event tüketir, per-`condition_id` `ClusterState` hesaplar, `SmartMoneyStrategy.attach_cluster_state()` çağırır

#### Pencere
Max 24 saat freshness, ama latency decay 30dk eşiğiyle ön-filtre.

#### Exit signal tracking
Tier 1 whale bizim açtığımız pozisyonu kapatıyorsa: `exit_pressure` sinyali aggregator'a, portfolio rebalancer (§17) bu sinyali tetikleyici olarak kullanır.

### 14.4 StatQuantStrategy
**Alt sinyaller (her biri ayrı strateji olarak da ele alınabilir):**
- Mean reversion: 1h fiyat hareketi > 2σ ise ters yönde küçük pozisyon
- Momentum: 6h trend > eşik + hacim onayı ise yönde
- Volatility breakout: Yatay konsolidasyondan sonra breakout
- Time decay arb: Resolution'a < 24h kala implied vs final probability uyumsuzluğu

### 14.5 FundamentalsStrategy
**Kategori bazlı modeller:**
- **Politics:** poll aggregator (538-style)
- **Sports:** Elo, head-to-head, injury report
- **Crypto:** TA + funding rate + exchange netflow
- **Macro:** ekonomik takvim, Fed dot plot

**Conviction:** Model olasılığı ile market olasılığı arasındaki absolute fark.

### 14.6 Aggregator
Her strateji sinyalini `strategy_signals`'a yazar. Aggregator bunları aynı `condition_id` için birleştirir:

```python
def aggregate(signals: list[StrategySignal]) -> Optional[AggregatedSignal]:
    if len(signals) == 0:
        return None
    # Yön çatışması varsa: sinyallerin conviction-weighted oylaması
    yes_score = sum(s.conviction for s in signals if s.action == "BUY_YES")
    no_score  = sum(s.conviction for s in signals if s.action == "BUY_NO")
    if abs(yes_score - no_score) < CONFLICT_THRESHOLD:
        return None  # belirsiz, pas geç
    direction = "BUY_YES" if yes_score > no_score else "BUY_NO"
    edge = weighted_average_edge(signals, direction)
    return AggregatedSignal(direction=direction, edge=edge, ...)
```

---

## 15. Risk motoru

### 15.1 Kelly sizing
```python
def quarter_kelly_size(p: float, market_price: float, bankroll: float,
                      hard_cap_pct: float = 0.05) -> float:
    """
    p             = bizim olasılığımız
    market_price  = mevcut taker fiyatı (0..1)
    """
    if p <= market_price:
        return 0.0
    b = (1 - market_price) / market_price
    q = 1 - p
    f_star = (b * p - q) / b
    f_used = max(0.0, min(f_star / 4, hard_cap_pct))   # quarter Kelly + hard cap
    return f_used * bankroll
```

### 15.2 Risk limitleri (config'den yüklenir)
```yaml
risk:
  kelly_fraction: 0.25                    # quarter Kelly
  max_position_pct_of_bankroll: 0.05
  max_exposure_per_category_pct: 0.30
  max_total_exposure_pct: 0.80            # nakit her zaman %20+
  daily_max_loss_pct: 0.05
  weekly_max_loss_pct: 0.10
  max_concentration_single_event_pct: 0.10
  max_open_positions: 50
  min_market_liquidity_usd: 10000         # daha düşükse pas
  max_position_pct_of_market_volume: 0.05 # market impact koruma
```

### 15.3 Kill-switch
Aşağıdaki herhangi biri tetiklendiğinde tüm yeni emirler durur ve mevcut pozisyonlar held edilir (veya politika `liquidate` ise kapatılır):
- Günlük kayıp > limit
- Anormal slippage > eşik (model bozulmuş olabilir)
- API hata oranı > %5
- Cüzdan bakiyesi beklenenden farklı (deposit/withdraw uyumsuzluğu)
- Manuel tetik (CLI veya Slack komutu)

### 15.4 RiskPolicy.evaluate()
Her aggregated signal bu fonksiyondan geçer:
```python
class RiskDecision(Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REDUCE = "reduce"   # boyut azalt

def evaluate(self, signal: AggregatedSignal, portfolio: Portfolio) -> RiskDecision:
    if self.kill_switch.is_engaged():
        return RiskDecision.REJECT
    if portfolio.daily_pnl_pct < -self.cfg.daily_max_loss_pct:
        return RiskDecision.REJECT
    if portfolio.total_exposure_pct + signal.size_pct > self.cfg.max_total_exposure_pct:
        return RiskDecision.REDUCE
    if portfolio.category_exposure(signal.category) + signal.size_pct > self.cfg.max_exposure_per_category_pct:
        return RiskDecision.REDUCE
    if signal.market_liquidity_usd < self.cfg.min_market_liquidity_usd:
        return RiskDecision.REJECT
    return RiskDecision.APPROVE
```

---

## 16. Execution motoru

### 16.1 Maker-first router
```python
class OrderRouter:
    async def route(self, decision: TradeDecision) -> Order:
        # 1. Maker olarak best_bid+1tick / best_ask-1tick koy
        # 2. T saniye bekle, kısmi fill izle
        # 3. Yine doluysa süreyi uzat
        # 4. Yine boşsa: yarıyı taker'a çevir, yarısını maker tut
        # 5. Final timeout'ta kalanı taker olarak doldur
```

Konfigürasyon:
```yaml
execution:
  maker_first: true
  maker_timeout_sec: 60
  partial_taker_fill_pct: 0.5
  max_slippage_bps: 50
  cancel_on_disconnect: true
```

### 16.2 Slippage modeli
Tarihsel fill'lerden öğrenilen basit model:
```
expected_slippage = a * (order_size / book_depth_at_5pct) ^ b
```
Eğer expected_slippage > max_slippage_bps → emri ya küçült ya iptal et.

### 16.3 Paper executor
- Gerçek emir göndermez
- Fill'i simüle eder: maker emirleri için bekleyen book'taki karşı taraf kadar dol, taker için anlık best price'a dol
- Her şeyi `our_orders` ile aynı tabloya yazar, `mode='paper'` ile

### 16.4 Live executor
- `py-clob-client-v2` üzerinden submit
- Order ID'leri DB'ye yazar
- WebSocket'ten user channel'ı izleyerek fill confirm

---

## 17. Portfolio manager

- **Ledger:** çift girişli muhasebe, her trade ledger'a yazılır
- **Mark-to-market:** her token için mevcut mid price ile pozisyonları her dakika yeniden değerlendir
- **NAV:** `nav = cash_balance + sum(qty × mark_price)`
- **P&L decomposition:** strateji bazında, kategori bazında, dönem bazında
- **Rebalancer:** Eğer bir pozisyonun mevcut tezisi geçersizleştiyse (örn. sinyal terse döndü) → exit emri

---

## 18. Backtest motoru

**Hedef:** Stratejileri canlıya çıkarmadan önce geçmiş veriyle test etmek.

### Bileşenler:
- **Historical replay:** `orderbook_snapshots` + `trades` + `news_signals` zaman sırasıyla yeniden oynatılır
- **Walk-forward analysis:** Train pencere → test pencere kayan
- **Metrikler:** Sharpe, Sortino, Calmar, max drawdown, win rate, profit factor, expectancy
- **Realistic frictions:** taker fee, maker rebate, slippage, latency simülasyonu (50–500ms randomize)

**Çıktı:** Markdown + HTML rapor, equity curve grafiği, strateji bazlı kırılım.

### Backtest kabul kriterleri (canlıya geçiş için):
- Min 3 ay (90 gün) historik pencere
- Sharpe > 1.5
- Max DD < %25
- Win rate > %52 (binary marketler için)
- 200+ trade örneklemi
- Walk-forward'da tutarlı performans (overfitting değil)

---

## 19. Paper trading modu

**Süre:** 4–8 hafta minimum.
**Sermaye:** Sanal $100K (gerçekçi pozisyon büyüklükleri test etmek için).
**Live feed:** Gerçek market verisi.
**Execution:** Simüle.
**Raporlama:** Gerçek live mode ile aynı dashboard.

### Live'a promote checklist (`scripts/promote_to_live.py`):
- [ ] Paper trading 30+ gün başarılı
- [ ] Sharpe > 1.2 (paper)
- [ ] Max DD < %20 (paper)
- [ ] Risk engine kill-switch en az 1 kez test edildi
- [ ] Reconnect/restart drill yapıldı (24h+ uptime)
- [ ] Cüzdan ve secret rotation prosedürü çalıştırıldı
- [ ] Slack/Telegram alert kanalı çalışır durumda
- [ ] Backup ve DB recovery test edildi
- [ ] Coğrafi/regülatif durum gözden geçirildi
- [ ] Initial live sermaye: paper NAV'ın %5'inden fazlasını koyma

---

## 20. Observability

**Logging:** `structlog` ile JSON formatında. Her log: `ts, level, service, strategy, condition_id, action, latency_ms, message`.

**Metrics (Prometheus):**
- `pm_nav_total` (gauge)
- `pm_position_count` (gauge)
- `pm_signal_emitted_total{strategy}` (counter)
- `pm_order_submitted_total{side, mode}` (counter)
- `pm_order_filled_total{strategy}` (counter)
- `pm_pnl_daily{strategy, category}` (gauge)
- `pm_api_latency_seconds{endpoint}` (histogram)
- `pm_kill_switch_engagements_total` (counter)

**Grafana dashboard'ları:**
1. **Overview** — NAV, daily P&L, open positions, kill-switch status
2. **Strategies** — her stratejinin sinyal-fill-pnl funnel'ı
3. **Markets** — en yoğun trade edilen marketler, edge dağılımı
4. **Infra** — DB connection pool, WebSocket lag, API errors

**Alerts (Slack/Telegram webhook):**
- Kill-switch engaged → 🚨 critical
- Daily loss > 3% → ⚠️ warn
- WebSocket disconnect > 60s → ⚠️ warn
- New position > $1K → ℹ️ info
- Backtest scheduled job failed → ⚠️ warn

---

## 21. Konfigürasyon ve secrets

**Hiyerarşi:** `base.yaml` → `paper.yaml`/`live.yaml` → env var override.

**Secrets** (`.env`, asla commit etme):
```env
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_PASSPHRASE=...
ALCHEMY_API_KEY=...
X_BEARER_TOKEN=...
GDELT_API_KEY=                            # opsiyonel, free
OPENAI_API_KEY=...                        # LLM sentiment için
ANTHROPIC_API_KEY=...                     # alternatif/yedek
SLACK_WEBHOOK_URL=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
POSTGRES_URL=postgresql://user:pass@db:5432/poly_meridian
REDIS_URL=redis://redis:6379/0
MODE=paper                                # paper / live-conservative / live-normal
```

**Secret yönetimi (prod):** Hashicorp Vault veya en azından AWS/GCP Secret Manager. Yerel dev'de `.env` yeterli.

---

## 22. Deployment (Docker Compose)

**`docker-compose.yml`:**
```yaml
version: "3.9"

services:
  db:
    image: timescale/timescaledb-pg16:latest-pg16
    environment:
      POSTGRES_USER: poly
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: poly_meridian
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./scripts/bootstrap_db.sh:/docker-entrypoint-initdb.d/init.sh
    ports: ["5432:5432"]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  agent:
    build: .
    depends_on: [db, redis]
    env_file: .env
    environment:
      MODE: ${MODE:-paper}
    volumes:
      - ./config:/app/config:ro
      - ./logs:/app/logs
    restart: unless-stopped
    command: ["python", "-m", "poly_meridian.main"]

  prometheus:
    image: prom/prometheus:latest
    volumes: ["./infra/prometheus.yml:/etc/prometheus/prometheus.yml"]
    ports: ["9090:9090"]

  grafana:
    image: grafana/grafana:latest
    ports: ["3000:3000"]
    volumes:
      - grafana_data:/var/lib/grafana
      - ./infra/grafana/dashboards:/etc/grafana/provisioning/dashboards

volumes:
  pgdata:
  grafana_data:
```

**Sunucu önerisi (başlangıç):**
- 4 vCPU, 8GB RAM, 100GB SSD VPS
- US East lokasyon (Polymarket API'leri New York'a yakın)
- Tahmini maliyet: $40–80/ay (Hetzner, Vultr, OVH)
- 7/24 uptime, otomatik restart

---

## 23. Test stratejisi

- **Unit tests:** Risk fonksiyonları, Kelly sizer, feature hesaplamaları, slippage modeli. Hedef coverage > %80.
- **Integration tests:** DB schema, Gamma API mock, CLOB API mock, WebSocket reconnect.
- **Strategy tests:** Her stratejiye synthetic veriyle pozitif ve negatif örnekler.
- **Replay tests:** Backtest motoruna 7 günlük tarihsel veri ver, sonucun deterministik olduğunu doğrula.
- **Chaos tests:** API timeout, DB disconnect, RPC fail simülasyonları — agent ayakta kalmalı.

CI: GitHub Actions üzerinden push'ta lint + unit + integration.

---

## 24. Roadmap ve milestones

### Phase 0 — Setup (Hafta 1)
- [ ] Repo, Docker compose, DB schema
- [ ] Gamma + CLOB + WebSocket client'ları
- [ ] Logger, config, secrets
- [ ] DB'ye 7 gün historical backfill

### Phase 1 — Data layer canlı (Hafta 2)
- [ ] Live WebSocket order book replication
- [ ] News (GDELT) ingestion
- [ ] Smart wallet tracker (basic)
- [ ] Feature engineering pipeline (orderbook + time)

### Phase 2 — İlk strateji + paper mode (Hafta 3–4)
- [ ] Arbitrage strategy + risk engine (limited)
- [ ] Paper executor
- [ ] Basic Grafana dashboard
- [ ] 1 hafta paper run, hata ayıklama

### Phase 3 — Sentiment + Smart Money (Hafta 5–6)
- [ ] LLM sentiment scoring (FinBERT veya GPT-class)
- [ ] News-market semantic matching (embedding)
- [ ] SentimentStrategy + SmartMoneyStrategy
- [ ] Aggregator
- [ ] 2 hafta paper run

### Phase 4 — StatQuant + Backtest (Hafta 7–8)
- [ ] Backtest motoru (replay)
- [ ] Walk-forward validation
- [ ] StatQuantStrategy alt-sinyalleri
- [ ] Tüm stratejilerin 90-gün backtest raporu

### Phase 5 — Fundamentals + Hardening (Hafta 9–10)
- [ ] Politics, Crypto, Sports model'leri
- [ ] Kill-switch testleri
- [ ] Chaos engineering test
- [ ] Full disaster recovery drill

### Phase 6 — Kademeli canlı (Hafta 11+)
- [ ] $500 ile live-conservative başla
- [ ] Her hafta performans değerlendir
- [ ] Hedeflere ulaşırsa sermayeyi 2x scale et
- [ ] 3 ay sonra $5K, 6 ay sonra $25K (planlı, koşullu)

---

## 25. Claude Code'a verilecek build prompt

> Aşağıdaki prompt'u `claude code` CLI'ye verince agent reposunu inşa etmeye başlar. Bu doküman (`POLY_MERIDIAN_MASTER.md`) repo köküne `/docs/MASTER_SPEC.md` olarak kopyalanmalıdır ki Claude Code her zaman erişebilsin.

```
ROLE: Sen Claude Code'sın. Sıfırdan bir Polymarket quant trading agent inşa
edeceksin. Tüm teknik spec /docs/MASTER_SPEC.md dosyasındadır. Her adımda
spec'e referans ver.

PRINCIPLES (sırasıyla uy):
1. Önce CONTRACTS, sonra IMPLEMENTATION. Her modülde önce ABC/Protocol yaz.
2. Type hints zorunlu (Python 3.12+, mypy --strict geçmeli).
3. Tüm I/O asyncio, sync kod sadece pure-compute yerlerde.
4. SECRETS asla repoya girmez. Sadece .env.example commit edilir.
5. Her PR'da: lint (ruff) + format (black) + type (mypy) + test (pytest).
6. Risk modülü dışındaki hiçbir modül emir gönderemez.
7. live mode'a geçiş için scripts/promote_to_live.py manuel onay gerektirir.

TASK ORDER (Phase 0'dan başla):
1. Phase 0: Repo iskeleti + docker-compose + DB schema + boş modül ABC'leri
2. Phase 1: Gamma + CLOB + WS clients + backfill + feature pipeline (skeleton)
3. Phase 2: ArbitrageStrategy + RiskEngine + PaperExecutor + Grafana

Her Phase sonunda:
- pytest geçmeli
- docker-compose up çalışmalı
- README'de Phase'in nasıl çalıştırıldığı belgelenmeli
- Bir kısa STATUS.md güncellenmeli (neler tamam, neler eksik)

CODE STYLE:
- Python 3.12, uv ile dependency mgmt
- pydantic v2 ile config ve domain models
- structlog ile JSON logging
- pytest + pytest-asyncio
- ruff (E,F,I,UP,B,SIM), black, mypy --strict

GUARDRAILS:
- Asla MODE=live ile başlama, default paper
- Asla hardcoded private key, API key
- Asla risk modülünü bypass edecek "geçici" kod yazma
- Asla yeterli test olmadan bir stratejiyi ToString.aggregator'a ekleme

START:
git init, pyproject.toml oluştur, docker-compose.yml, bootstrap DB. Phase 0'a
başla.
```

---

## EK A — Sayılarla Polymarket (girişimci dashboard'u)

| KPI                                | Değer (Mayıs 2026)         |
|------------------------------------|----------------------------|
| Aylık hacim (Nisan 2026)           | $10.3B                     |
| Q1 2026 toplam hacim                | $26.2B                     |
| Aylık aktif cüzdan                 | 840,000 (Şubat 2026)       |
| Aktif market sayısı                | binlerce (long-tail)       |
| Min taker fee (geopolitics)         | %0                         |
| Max taker fee (crypto)              | %1.80                      |
| US platformu                       | %0.30 taker, %0.20 maker rebate |
| Maker rebate (politics/tech)       | ~%25 of taker              |
| Tek-market arbitraj oranı (akademi) | %41                        |
| Top trader lifetime (örnek)        | $1M – $85M                 |
| API rate limit (Gamma)             | 15K req / 10sn             |
| WebSocket heartbeat                | 10sn                       |

## EK B — Bizim hedef metrikleri

| Metrik                          | Hedef (Yıl 1)              |
|---------------------------------|----------------------------|
| Net yıllık getiri               | %40–80                     |
| Sharpe Ratio                    | > 1.5                      |
| Max Drawdown                    | < %25                      |
| Win Rate (binary)               | > %55                      |
| Profit Factor                   | > 1.8                      |
| Median trade hold süresi        | 2–10 gün                   |
| Aktif pozisyon sayısı           | 10–40                      |
| Strateji başına edge (avg)      | %2–6 per opportunity        |
| Op. uptime                      | %99+                       |
| Paper→Live geçiş öncesi min süre| 4–8 hafta                  |

---

## EK C — Glossary

- **CLOB** — Central Limit Order Book; klasik borsa motoru
- **Maker** — order book'a likidite koyan (limit order)
- **Taker** — likiditeyi alan (market order)
- **YES/NO Token** — outcome token, $0–$1 arası
- **Resolution** — marketin sonuçlanması; $0 veya $1'e settle
- **Edge** — bizim olasılığımız ile market olasılığı farkı
- **Conviction** — bir sinyale ne kadar güvendiğimiz (0–1)
- **NAV** — Net Asset Value, portföyün toplam değeri
- **MTM** — Mark-to-Market, anlık piyasa değeri
- **Drawdown** — zirveden dibe düşüş yüzdesi
- **Sharpe** — risk-ayarlı getiri (return / vol)
- **Slippage** — istenen fiyat ile gerçekleşen fiyat arasındaki fark
- **Negative-risk market** — bir event seti içinde token'lar toplamı = $1.00
- **UMA** — Polymarket'in resolution oracle'ı

---

**SON.** Bu doküman canlı tutulacak. Her major değişiklikte versiyonu bump et ve `/docs/CHANGELOG.md`'ye kaydet.

