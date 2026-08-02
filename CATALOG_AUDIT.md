# Catalog data audit

Every place the decant sheet holds the same product under two rows — a misspelling, a second sheet, a stray word. Across 1,354 products.

The bot is not wrong to show two cards for these: it has two products and no way to know they are one. Ordered below by what each costs.

**How duplicates are resolved on import:** the men's/unisex sheet wins over every other sheet, and within a sheet the row that comes first wins. Testers are never read — they are stock-on-hand, not a price list. Locked by `tests/test_import_precedence.py`.

| | finding | count |
|---|---|---|
| 🔴 | [Same size, two different prices](#same-size-two-different-prices) | **39** |
| 🟠 | [One copy lists more sizes](#one-copy-lists-more-sizes) | 10 |
| 🟠 | [Sizes dropped by the men's-sheet rule](#sizes-dropped-by-the-mens-sheet-rule) | 13 |
| 🟠 | [One perfume, two spellings](#one-perfume-two-spellings) | 8 |
| 🟠 | [A brand spelled two ways](#a-brand-spelled-two-ways) | 2 |
| 🔵 | [One row carries a word the other does not](#one-row-carries-a-word-the-other-does-not) | 18 |
| ⚪ | [Listed twice at the same prices](#listed-twice-at-the-same-prices) | 39 |
| ⚪ | [Name text the customer sees](#name-text-the-customer-sees) | 15 |

Regenerate with `python scripts/catalog_audit.py --md CATALOG_AUDIT.md`.

---

## Same size, two different prices

The same size priced two ways. The importer keeps the first copy it reads, so the other price never reaches the bot — whichever row happens to come first is what every customer is quoted. **Nobody can work these out from the sheet; each one needs someone to say which price is right.** Bold cells are where the two disagree.

### Ahmed Al Maghribi Azure Royal

`Decant Sheet Men-UNISEX row 47`  ·  `For Women row 6`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 130 | 180 | 260 | 330 | 580 | — |
| discarded | **—** | **150** | **220** | **280** | **520** | **750** |

### Ahmed Al Maghribi Ignite Oud

`Decant Sheet Men-UNISEX row 50`  ·  `For Women row 9`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 230 | 350 | 520 | 660 | 1,220 | 1,730 |
| discarded | **190** | **290** | **440** | **560** | **1,080** | **1,580** |

### Ahmed Al Maghribi Rose Noir

`Decant Sheet Men-UNISEX row 61`  ·  `For Women row 11`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 160 | 230 | 340 | 430 | 760 | 1,080 |
| discarded | **—** | **210** | **320** | **380** | 760 | 1,080 |

### Ahmed Al Maghribi Summer Oud

`Decant Sheet Men-UNISEX row 49`  ·  `For Women row 8`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 210 | 310 | 470 | 590 | 1,080 | 1,580 |
| discarded | **190** | **290** | **440** | **560** | 1,080 | 1,580 |

### Al Wataniah Kayaan Classic

`Decant Sheet Men-UNISEX row 88`  ·  `For Women row 15`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 140 | 190 | 280 | 350 | 580 | 800 |
| discarded | **—** | **180** | **270** | **340** | **620** | **850** |

### Arabiyat Prestige Safa

`Decant Sheet Men-UNISEX row 170`  ·  `For Women row 36`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 160 | 220 | 320 | 400 | 720 | 980 |
| discarded | **150** | **210** | **310** | **380** | 720 | 980 |

### Armaf Odyssey Toffee Coffee

`Decant Sheet Men-UNISEX row 236`  ·  `For Women row 34`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 130 | 170 | 240 | 300 | 540 | 700 |
| discarded | **—** | **150** | **220** | **280** | **520** | **750** |

### Bvlgari Glacial Essence

`Decant Sheet Men-UNISEX row 826`  ·  `Decant Sheet Men-UNISEX row 830`

| copy | 3ml | 5ml | 8ml | 10ml |
|---|---|---|---|---|
| **in the catalog** | 250 | 390 | 610 | 740 |
| discarded | **290** | **430** | **670** | **820** |

### French Avenue Cocoa Morado

`Decant Sheet Men-UNISEX row 354`  ·  `For Women row 54`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 150 | 210 | 310 | 390 | 720 | 980 |
| discarded | **130** | **180** | **260** | **350** | **650** | **900** |

### Guerlain Mon Guerlain EDP

`For Women row 215`  ·  `For Women row 220`

| copy | 3ml | 5ml | 8ml | 10ml |
|---|---|---|---|---|
| **in the catalog** | 250 | 370 | 560 | 690 |
| discarded | **290** | **440** | **670** | **830** |

### Hugo Boss Boss Bottled Absolu

`Decant Sheet Men-UNISEX row 956`  ·  `Decant Sheet Men-UNISEX row 965`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 250 | 380 | 600 | 750 | 1,400 | 2,100 |
| discarded | **340** | **520** | **810** | **1,000** | **—** | **—** |

### Ibraheem AlQurashi French Tobacco

`Decant Sheet Men-UNISEX row 393`  ·  `Decant Sheet Men-UNISEX row 416`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 170 | 250 | 370 | 460 | — | — |
| discarded | **150** | **200** | **300** | **380** | **700** | **950** |

### Khadlaj Caffe Latte

`Decant Sheet Men-UNISEX row 428`  ·  `For Women row 63`

| copy | 3ml | 5ml | 8ml | 10ml |
|---|---|---|---|---|
| **in the catalog** | 120 | 150 | 220 | 280 |
| discarded | **—** | 150 | 220 | **270** |

### Khadlaj Mocha Latte

`Decant Sheet Men-UNISEX row 427`  ·  `For Women row 61`

| copy | 3ml | 5ml | 8ml | 10ml |
|---|---|---|---|---|
| **in the catalog** | 120 | 150 | 220 | 280 |
| discarded | **—** | 150 | 220 | **270** |

### Lattafa Emeer

`Decant Sheet Men-UNISEX row 495`  ·  `For Women row 72`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 170 | 240 | 360 | 450 | 850 | 1,170 |
| discarded | **160** | **220** | **330** | **400** | **800** | **1,200** |

### Lattafa Jasoor

`Decant Sheet Men-UNISEX row 458`  ·  `For Women row 91`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 140 | 190 | 270 | 340 | 600 | — |
| discarded | **—** | **180** | **250** | 340 | **640** | **860** |

### Lattafa Khamrah

`Decant Sheet Men-UNISEX row 454`  ·  `For Women row 80`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 160 | 230 | 330 | 420 | 780 | 1,070 |
| discarded | **—** | **190** | **300** | **370** | **680** | **930** |

### Lattafa Khamrah Qahwa

`Decant Sheet Men-UNISEX row 452`  ·  `For Women row 79`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 160 | 230 | 330 | 420 | 780 | 1,070 |
| discarded | **—** | **190** | **300** | **370** | **680** | **930** |

### Lattafa Nebras

`Decant Sheet Men-UNISEX row 446`  ·  `For Women row 89`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 140 | 190 | 300 | 370 | 700 | 970 |
| discarded | **—** | **200** | 300 | **380** | **660** | **920** |

### Lattafa Nebras Elixir

`Decant Sheet Men-UNISEX row 463`  ·  `For Women row 90`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 140 | 190 | 300 | 370 | — | — |
| discarded | **—** | **200** | 300 | **380** | **660** | **920** |

### Lattafa Teriaq Intense

`Decant Sheet Men-UNISEX row 497`  ·  `For Women row 75`

| copy | 3ml | 5ml | 8ml | 10ml |
|---|---|---|---|---|
| **in the catalog** | 150 | 210 | 310 | 390 |
| discarded | **—** | **170** | **240** | **320** |

### Maison Alhambra Bright Peach

`Decant Sheet Men-UNISEX row 533`  ·  `For Women row 98`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 130 | 180 | 250 | 310 | 580 | 710 |
| discarded | **120** | **160** | **220** | **280** | **500** | **700** |

### Maison Alhambra Como Moiselle

`Decant Sheet Men-UNISEX row 531`  ·  `For Women row 96`  ·  `For Women row 101`

| copy | 5ml | 8ml | 10ml | 30ml |
|---|---|---|---|---|
| **in the catalog** | 130 | 180 | 240 | 510 |
| discarded | **150** | **200** | **280** | **570** |
| discarded | 130 | **190** | 240 | **690** |

### Maison Alhambra Lovely Cherry

`Decant Sheet Men-UNISEX row 536`  ·  `For Women row 94`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml |
|---|---|---|---|---|---|
| **in the catalog** | 130 | 180 | 250 | 310 | 580 |
| discarded | **120** | **160** | **220** | **280** | **500** |

### Maison Alhambra Porto Neroli

`Decant Sheet Men-UNISEX row 532`  ·  `For Women row 97`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 130 | 180 | 250 | 310 | 580 | — |
| discarded | **120** | **160** | **220** | **280** | **500** | **800** |

### Maison Alhambra Rose Petals

`Decant Sheet Men-UNISEX row 537`  ·  `For Women row 95`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 130 | 180 | 250 | 310 | 580 | — |
| discarded | **120** | **170** | **240** | 310 | **540** | **710** |

### Mancera Cedrat Boise

`Decant Sheet Men-UNISEX row 1023`  ·  `For Women row 256`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 280 | 420 | 640 | 790 | 1,420 | 2,050 |
| discarded | **240** | **360** | **520** | **660** | **—** | **—** |

### Mancera Instant Crush

`Decant Sheet Men-UNISEX row 1020`  ·  `For Women row 255`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 280 | 420 | 640 | 790 | 1,420 | 2,050 |
| discarded | **260** | **370** | **560** | **660** | **—** | **—** |

### Paco Rabanne Pure XS

`Decant Sheet Men-UNISEX row 1080`  ·  `Decant Sheet Men-UNISEX row 1088`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 160 | 240 | 380 | 480 | — | — |
| discarded | **200** | **290** | **450** | **550** | **940** | **1,330** |

### Paris Corner December Vanilla

`Decant Sheet Men-UNISEX row 600`  ·  `For Women row 114`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | — | 150 | 210 | 280 | — | 720 |
| discarded | **120** | **170** | **240** | **310** | **540** | **710** |

### Paris Corner Emir Celestial

`Decant Sheet Men-UNISEX row 612`  ·  `Decant Sheet Men-UNISEX row 632`

| copy | 3ml | 5ml | 8ml | 10ml |
|---|---|---|---|---|
| **in the catalog** | — | 140 | 200 | 270 |
| discarded | **130** | **190** | **270** | **340** |

### Paris Corner Emir Cherry Cola

`Decant Sheet Men-UNISEX row 611`  ·  `For Women row 117`

| copy | 5ml | 8ml | 10ml | 30ml |
|---|---|---|---|---|
| **in the catalog** | 150 | 210 | 280 | 759 |
| discarded | 150 | **220** | 280 | **—** |

### Paris Corner Eternal coffee

`Decant Sheet Men-UNISEX row 586`  ·  `For Women row 113`

| copy | 5ml | 8ml | 10ml | 30ml |
|---|---|---|---|---|
| **in the catalog** | 130 | 190 | 240 | 550 |
| discarded | 130 | 190 | 240 | **880** |

### Pendora Scents Enchantment Blue Intense

`Decant Sheet Men-UNISEX row 638`  ·  `For Women row 130`

| copy | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|
| **in the catalog** | 130 | 180 | 240 | 420 | 550 |
| discarded | 130 | **190** | 240 | **—** | **—** |

### Rasasi Hawas Diva

`Decant Sheet Men-UNISEX row 653`  ·  `For Women row 140`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 150 | 200 | 300 | 380 | — | — |
| discarded | **—** | **180** | **260** | **340** | **620** | **860** |

### Zimaya Sharaf Blend

`Decant Sheet Men-UNISEX row 744`  ·  `For Women row 147`

| copy | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|
| **in the catalog** | 150 | 220 | 280 | 520 | 740 |
| discarded | **159** | **229** | **289** | **—** | **—** |

### Zimaya Tiramisu Caramel

`Decant Sheet Men-UNISEX row 759`  ·  `For Women row 152`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 130 | 190 | 270 | 340 | 560 | 740 |
| discarded | 130 | **180** | **250** | **310** | **—** | **—** |

### Zimaya Tiramisu Coco

`Decant Sheet Men-UNISEX row 758`  ·  `For Women row 151`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 130 | 190 | 270 | 340 | 560 | 740 |
| discarded | 130 | **180** | **250** | **310** | **—** | **—** |

### Zimaya Tiramisu S'mores

`Decant Sheet Men-UNISEX row 757`  ·  `For Women row 153`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 130 | 190 | 270 | 340 | 560 | 740 |
| discarded | 130 | **180** | **250** | **310** | **—** | **—** |

---

## One copy lists more sizes

No contradiction here — every size both copies list agrees on the price. One copy is simply missing rows, and the sizes only it has are being dropped on import. **Merge these rather than choosing between them**: keep every size either copy offers.

### Afnan Cherry Bouquet

`Decant Sheet Men-UNISEX row 17`  ·  `For Women row 26`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 190 | 260 | 400 | 490 | 940 | 1,340 |
| discarded | 190 | 260 | 400 | 490 | **—** | **—** |

### Afnan Delicious Bouquet

`Decant Sheet Men-UNISEX row 16`  ·  `For Women row 25`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 190 | 260 | 400 | 490 | 940 | 1,340 |
| discarded | 190 | 260 | 400 | 490 | **—** | **—** |

### Afnan Mystique Bouquet

`Decant Sheet Men-UNISEX row 18`  ·  `For Women row 27`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 190 | 260 | 400 | 490 | 940 | 1,340 |
| discarded | 190 | 260 | 400 | 490 | **—** | **—** |

### Afnan Rare tiffany

`Decant Sheet Men-UNISEX row 34`  ·  `For Women row 28`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 130 | 170 | 250 | 320 | 580 | 800 |
| discarded | **—** | 170 | 250 | 320 | 580 | 800 |

### Ahmed Al Maghribi AHL

`Decant Sheet Men-UNISEX row 42`  ·  `For Women row 5`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 240 | 360 | 540 | 680 | 1,300 | — |
| discarded | 240 | 360 | 540 | 680 | 1,300 | **1,850** |

### Arabiyat Prestige Swar Venin

`Decant Sheet Men-UNISEX row 180`  ·  `For Women row 37`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 150 | 200 | 300 | 380 | 680 | 940 |
| discarded | 150 | 200 | 300 | 380 | 680 | **—** |

### Lattafa Mashrabya

`Decant Sheet Men-UNISEX row 464`  ·  `For Women row 77`

| copy | 3ml | 5ml | 8ml | 10ml | 30ml |
|---|---|---|---|---|---|
| **in the catalog** | 130 | 170 | 240 | 320 | — |
| discarded | **—** | 170 | 240 | 320 | **840** |

### Paris Corner Emir Fire Your Desire

`Decant Sheet Men-UNISEX row 623`  ·  `For Women row 122`

| copy | 5ml | 8ml | 10ml | 30ml |
|---|---|---|---|---|
| **in the catalog** | 130 | 190 | 240 | 769 |
| discarded | 130 | 190 | 240 | **—** |

### Rasasi Hawas Pink

`Decant Sheet Men-UNISEX row 652`  ·  `For Women row 137`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 150 | 210 | 300 | 380 | 760 | 1,000 |
| discarded | **—** | 210 | 300 | 380 | **—** | **—** |

### Zimaya Musk Is Great

`Decant Sheet Men-UNISEX row 754`  ·  `For Women row 149`

| copy | 3ml | 5ml | 8ml | 10ml | 20ml | 30ml |
|---|---|---|---|---|---|---|
| **in the catalog** | 110 | 150 | 210 | 270 | 480 | — |
| discarded | 110 | 150 | 210 | 270 | 480 | **610** |

---

## One perfume, two spellings

Two rows whose names differ only by a typo inside one word.

| row A | row B | differs | prices |
|---|---|---|---|
| Franck Oliver Oud Touch | Frank Oliver Oud Touch | 'franck' vs 'frank' | **different** |
| Lattafa Fahad | Lattafa Fakhar | 'fahad' vs 'fakhar' | **different** |
| Lattafa Bade'e Al Oud Amethyst | Lattafa Bade'e Al Oud Amythyst | 'amethyst' vs 'amythyst' | **different** |
| Paris Corner Emir Just Bergamot | Paris Corner Emir Just Bergamont | 'bergamot' vs 'bergamont' | **different** |
| Paris Corner Emir Frenetic Deliceuse | Paris Corner Emir Frenetic Delicieuse | 'deliceuse' vs 'delicieuse' | **different** |
| Pendora Scents Classico De Profondo | Pendora Scents Clasico De Profondo | 'classico' vs 'clasico' | **different** |
| Swiss Arabian Shaghaf  Oud | Swiss Arabian Shagaf Oud | 'shaghaf' vs 'shagaf' | **different** |
| Swiss Arabian Shaghaf Oud Tonka | Swiss Arabian Shagaf Oud Tonka | 'shaghaf' vs 'shagaf' | same |

Price detail for the pairs that disagree:

- **Franck Oliver Oud Touch** — {'3ml': 120, '5ml': 170, '8ml': 240, '10ml': 310, '20ml': 540}
- **Frank Oliver Oud Touch** — {'3ml': 110, '5ml': 150, '8ml': 210, '10ml': 280, '20ml': 520, '30ml': 680}

- **Lattafa Fahad** — {'3ml': 180, '5ml': 260, '8ml': 390, '10ml': 490, '20ml': 900}
- **Lattafa Fakhar** — {'3ml': 130, '5ml': 170, '8ml': 240, '10ml': 300, '20ml': 540, '30ml': 700}

- **Lattafa Bade'e Al Oud Amethyst** — {'3ml': 140, '5ml': 190, '8ml': 270, '10ml': 340, '20ml': 600, '30ml': 800, '100ml_full': 2550}
- **Lattafa Bade'e Al Oud Amythyst** — {'5ml': 149, '8ml': 219, '10ml': 269, '30ml': 759}

- **Paris Corner Emir Just Bergamot** — {'5ml': 130, '8ml': 190, '10ml': 240, '30ml': 579, '100ml_full': 1650}
- **Paris Corner Emir Just Bergamont** — {'5ml': 130, '8ml': 190, '10ml': 240, '30ml': 580}

- **Paris Corner Emir Frenetic Deliceuse** — {'5ml': 150, '8ml': 220, '10ml': 280, '20ml': 480, '30ml': 700}
- **Paris Corner Emir Frenetic Delicieuse** — {'5ml': 150, '8ml': 210, '10ml': 280, '100ml_full': 1650}

- **Pendora Scents Classico De Profondo** — {'5ml': 130, '8ml': 190, '10ml': 240, '20ml': 420, '30ml': 550}
- **Pendora Scents Clasico De Profondo** — {'5ml': 130, '8ml': 180, '10ml': 240, '20ml': 420, '30ml': 550}

- **Swiss Arabian Shaghaf  Oud** — {'3ml': 170, '5ml': 230, '8ml': 350, '10ml': 440, '30ml': 1075, '75ml_full': 2950}
- **Swiss Arabian Shagaf Oud** — {'5ml': 230, '8ml': 350, '10ml': 440}

---

## A brand spelled two ways

Not a per-product decision — pick the correct spelling and replace it down the whole column.

| spelling | rows | spelling | rows |
|---|---|---|---|
| Arabiyat Presitige | 12 | Arabiyat Prestige | 39 |
| Caroline Herrera | 8 | Carolina Herrera | 3 |

---

## One row carries a word the other does not

The grey zone. Most are probably real flankers — Ana Abiyedh and Ana Abiyedh Rouge are two perfumes. A few look like one product written twice. Worth a glance, not a sweep.

| row A | row B | difference | prices |
|---|---|---|---|
| Arabiyat Prestige Raaes | Arabiyat Prestige Raaes Aurum | one row adds 'aurum' | same |
| Arabiyat Prestige Oud Al Layal | Arabiyat Prestige Oud Al Layal Aswad | one row adds 'aswad' | same |
| Arabiyat Prestige Oud Al Layal | Arabiyat Prestige Oud Al Layal Midnight . | one row adds 'midnight' | **different** |
| Armaf Odyssey Mandarin Sky | Armaf Odyssey Mandarin Sky Vintage | one row adds 'vintage' | **different** |
| Fragrance Word Royal Blend | Fragrance Word Royal Blend Vintage | one row adds 'vintage' | same |
| Fragrance Word Royal Blend | Fragrance Word Royal Blend Nero | one row adds 'nero' | same |
| Fragrance Word Royal Blend | Fragrance Word Royal Blend Bourbon | one row adds 'bourbon' | same |
| Fragrance Word Royal Blend | Fragrance Word Royal Blend Sequoia | one row adds 'sequoia' | **different** |
| Fragrance World Proud of You Tobbaco | Fragrance World Proud of You | one row adds 'tobbaco' | same |
| Lattafa Khamrah | Lattafa Khamrah Waha | one row adds 'waha' | **different** |
| Lattafa Ana Abiyedh | Lattafa Ana Abiyedh Rouge | one row adds 'rouge' | **different** |
| Lattafa Ana Abiyedh | Lattafa Ana Abiyedh Coral | one row adds 'coral' | **different** |
| Paris Corner Rifaaqat Adorn | Paris Corner Rifaaqat | one row adds 'adorn' | **different** |
| Pendora Scents Milano | Pendora Scents Milano Prive | one row adds 'prive' | same |
| Swiss Arabian Shaghaf  Oud | Swiss Arabian Shaghaf Oud Tonka | one row adds 'tonka' | **different** |
| Swiss Arabian Shagaf Oud | Swiss Arabian Shagaf Oud Tonka | one row adds 'tonka' | **different** |
| Givenchy L’Interdit EDP | Givenchy L’Interdit Rouge EDP | one row adds 'rouge' | same |
| Givenchy L’Interdit Rouge Ultime EDP | Givenchy L’Interdit Rouge EDP | one row adds 'ultime' | same |

---

## Listed twice at the same prices

Nothing is being lost today — the same product simply appears on two sheets with matching prices. Worth tidying only so the two cannot drift apart later, which is how the price conflicts at the top of this file happened.

| product | where |
|---|---|
| Ahmed Al Maghribi Oud & Roses | `Decant Sheet Men-UNISEX row 48` · `For Women row 7` |
| Ahmed Al Maghribi Zeleny | `Decant Sheet Men-UNISEX row 44` · `For Women row 10` |
| Al Rehab Choco Musk | `Decant Sheet Men-UNISEX row 132` · `For Women row 12` |
| Al Rehab Choco Musk Marshmallow | `Decant Sheet Men-UNISEX row 133` · `For Women row 13` |
| Al Rehab French Coffee | `Decant Sheet Men-UNISEX row 134` · `For Women row 14` |
| Albait Aldimashqi Chanel no 5 | `Decant Sheet Men-UNISEX row 116` · `For Women row 24` |
| Albait Aldimashqi Declaration | `Decant Sheet Men-UNISEX row 118` · `For Women row 19` |
| Albait Aldimashqi Goddess Burberry | `Decant Sheet Men-UNISEX row 119` · `For Women row 20` |
| Albait Aldimashqi Mademoiselle | `Decant Sheet Men-UNISEX row 121` · `For Women row 22` |
| Albait Aldimashqi Miss blooming bouquet | `Decant Sheet Men-UNISEX row 120` · `For Women row 21` |
| Albait Aldimashqi Paradox EDP | `Decant Sheet Men-UNISEX row 115` · `For Women row 23` |
| Albait Aldimashqi Poison girl | `Decant Sheet Men-UNISEX row 117` · `For Women row 17` |
| Arabiyat Presitige Sugarcane Vanilla | `Decant Sheet Men-UNISEX row 145` · `For Women row 41` |
| Burberry Her EDP Intense | `For Women row 165` · `New Decant Additions row 105` |
| Burberry Hero Parfum Intense | `Decant Sheet Men-UNISEX row 822` · `New Decant Additions row 52` |
| Chanel Bleu De Chanel Parfum | `Decant Sheet Men-UNISEX row 845` · `New Decant Additions row 53` |
| Chanel Coromandel | `Decant Sheet Men-UNISEX row 847` · `For Women row 180` |
| Dunhill Egyptian Smoke | `Decant Sheet Men-UNISEX row 893` · `New Decant Additions row 55` |
| Dunhill Indian Sandalwood | `Decant Sheet Men-UNISEX row 892` · `New Decant Additions row 54` |
| Dyptique Do Son EDP | `For Women row 195` · `New Decant Additions row 106` |
| French Avenue Zenith Vanilla | `Decant Sheet Men-UNISEX row 316` · `For Women row 55` |
| Guerlain Aqua Allegoria Rosa Rossa | `Decant Sheet Men-UNISEX row 937` · `For Women row 223` |
| Lalique Le Parfum | `Decant Sheet Men-UNISEX row 1001` · `For Women row 236` |
| Lattafa Eclaire | `Decant Sheet Men-UNISEX row 447` · `For Women row 78` |
| Lattafa Musamam White Intense | `Decant Sheet Men-UNISEX row 467` · `For Women row 87` |
| Maison Alhambra Kismet Magic | `Decant Sheet Men-UNISEX row 525` · `For Women row 102` |
| Maison Asrar Coffee Blend | `Decant Sheet Men-UNISEX row 549` · `For Women row 103` |
| Maison Asrar Unsolved Mystery | `Decant Sheet Men-UNISEX row 559` · `For Women row 107` |
| Maison Asrar Vanilla Aura | `Decant Sheet Men-UNISEX row 550` · `For Women row 105` |
| Maison Asrar Vanilla Voyage | `Decant Sheet Men-UNISEX row 551` · `For Women row 106` |
| Maison Asrar Veyra | `Decant Sheet Men-UNISEX row 560` · `For Women row 108` |
| Missoni Missoni Wave | `Decant Sheet Men-UNISEX row 1050` · `New Decant Additions row 56` |
| Montale Risterreto Intense Cafe | `Decant Sheet Men-UNISEX row 1058` · `For Women row 248` |
| Parfums de Marly Delina Exclusif | `For Women row 268` · `New Decant Additions row 107` |
| Paris Corner Khair | `Decant Sheet Men-UNISEX row 595` · `For Women row 123` |
| Paris Corner Khair Confection | `Decant Sheet Men-UNISEX row 598` · `For Women row 126` |
| Paris Corner Khair Felicity | `Decant Sheet Men-UNISEX row 599` · `For Women row 127` |
| Paris Corner Khair Fusion | `Decant Sheet Men-UNISEX row 597` · `For Women row 125` |
| Paris Corner Khair Pistachio | `Decant Sheet Men-UNISEX row 596` · `For Women row 124` |

---

## Name text the customer sees

Display names are printed to WhatsApp exactly as written, doubled spaces and status notes included. A customer asking for Tom Ford Noir gets a card headed `TOM FORD NOIR EDP(DISCONTINUED GEM)`.

| name | problem |
|---|---|
| `Al Haramain Detour intense  Noir` | doubled space |
| `Armaf Club de Nuit  Sillage` | doubled space |
| `Paris Corner Emir Vibrant Vetiver(Out of Stock)` | status note in the name ('out of stock') |
| `Swiss Arabian Shaghaf  Oud` | doubled space |
| `Armani Code Colonia(Discontinued Gem)` | status note in the name ('discontinued') |
| `Armani Code Absolu(Discontinued Gem)` | status note in the name ('discontinued') |
| `Azzaro Wanted By Night(Discontinued Gem)` | status note in the name ('discontinued') |
| `Bottega Venetta Pour Homme (Discontinued Gem)` | status note in the name ('discontinued') |
| `Guerlain L'Homme Ideal Platine Prive(Discontinued Gem)` | status note in the name ('discontinued') |
| `Mancera Black to Black(Discontinued Gem)` | status note in the name ('discontinued') |
| `Mont Blanc Legend  EDP` | doubled space |
| `Paco Rabanne 1 Million Lucky(Discontinued Gem)` | status note in the name ('discontinued') |
| `Tom Ford Noir EDP(Discontinued Gem)` | status note in the name ('discontinued') |
| `Valentino Uomo Intense EDP(Out of Stock)` | status note in the name ('out of stock') |
| `Valentino Valentina Myrrh Assoluto (Discontinued Gem)` | status note in the name ('discontinued') |

---

## What this deliberately leaves out

Concentration and flanker siblings. Dior Sauvage EDT and Dior Sauvage EDP are one edit apart and are two real perfumes at two real prices; so are Asad and Asad Elixir. Reporting those would bury everything above under a hundred false ones, so any pair whose only difference is a concentration word is treated as two genuine products.
