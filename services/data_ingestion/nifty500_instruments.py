"""
services/data_ingestion/nifty500_instruments.py
───────────────────────────────────────────────
Nifty 500 stock universe with NSE trading symbols.

Covers:
  - Nifty 50        (large cap flagship index)
  - Nifty Next 50   (large cap extended)
  - Nifty Midcap 150
  - Nifty Smallcap ~250 (most liquid subset)

Format: (trading_symbol, company_name, sector)

Kite instrument tokens are fetched dynamically from Kite Connect's
/instruments endpoint — tokens change on corporate actions/splits.
Run `python -m services.data_ingestion.nifty500_instruments` to
refresh tokens after getting your API key.
"""
from __future__ import annotations

# ─── Nifty 500 constituents (as of April 2025) ───────────────────────────────
# Format: (trading_symbol, company_name, sector)
NIFTY500: list[tuple[str, str, str]] = [
    # ── Nifty 50 ─────────────────────────────────────────────────────────────
    ("ADANIENT",     "Adani Enterprises Ltd",                       "Energy"),
    ("ADANIPORTS",   "Adani Ports and Special Economic Zone Ltd",   "Industrials"),
    ("APOLLOHOSP",   "Apollo Hospitals Enterprise Ltd",             "Healthcare"),
    ("ASIANPAINT",   "Asian Paints Ltd",                            "Materials"),
    ("AXISBANK",     "Axis Bank Ltd",                               "Financials"),
    ("BAJAJ-AUTO",   "Bajaj Auto Ltd",                              "Consumer Discretionary"),
    ("BAJAJFINSV",   "Bajaj Finserv Ltd",                           "Financials"),
    ("BAJFINANCE",   "Bajaj Finance Ltd",                           "Financials"),
    ("BHARTIARTL",   "Bharti Airtel Ltd",                           "Communication Services"),
    ("BPCL",         "Bharat Petroleum Corporation Ltd",            "Energy"),
    ("BRITANNIA",    "Britannia Industries Ltd",                    "Consumer Staples"),
    ("CIPLA",        "Cipla Ltd",                                   "Healthcare"),
    ("COALINDIA",    "Coal India Ltd",                              "Energy"),
    ("DIVISLAB",     "Divi's Laboratories Ltd",                     "Healthcare"),
    ("DRREDDY",      "Dr. Reddy's Laboratories Ltd",               "Healthcare"),
    ("EICHERMOT",    "Eicher Motors Ltd",                           "Consumer Discretionary"),
    ("ETERNAL",      "Eternal Ltd (Zomato)",                        "Consumer Discretionary"),
    ("GRASIM",       "Grasim Industries Ltd",                       "Materials"),
    ("HCLTECH",      "HCL Technologies Ltd",                        "Information Technology"),
    ("HDFCBANK",     "HDFC Bank Ltd",                               "Financials"),
    ("HDFCLIFE",     "HDFC Life Insurance Company Ltd",             "Financials"),
    ("HEROMOTOCO",   "Hero MotoCorp Ltd",                           "Consumer Discretionary"),
    ("HINDALCO",     "Hindalco Industries Ltd",                     "Materials"),
    ("HINDUNILVR",   "Hindustan Unilever Ltd",                      "Consumer Staples"),
    ("ICICIBANK",    "ICICI Bank Ltd",                              "Financials"),
    ("INDUSINDBK",   "IndusInd Bank Ltd",                           "Financials"),
    ("INFY",         "Infosys Ltd",                                 "Information Technology"),
    ("ITC",          "ITC Ltd",                                     "Consumer Staples"),
    ("JSWSTEEL",     "JSW Steel Ltd",                               "Materials"),
    ("KOTAKBANK",    "Kotak Mahindra Bank Ltd",                     "Financials"),
    ("LT",           "Larsen & Toubro Ltd",                         "Industrials"),
    ("M&M",          "Mahindra & Mahindra Ltd",                     "Consumer Discretionary"),
    ("MARUTI",       "Maruti Suzuki India Ltd",                     "Consumer Discretionary"),
    ("NESTLEIND",    "Nestle India Ltd",                            "Consumer Staples"),
    ("NTPC",         "NTPC Ltd",                                    "Utilities"),
    ("ONGC",         "Oil & Natural Gas Corporation Ltd",           "Energy"),
    ("POWERGRID",    "Power Grid Corporation of India Ltd",         "Utilities"),
    ("RELIANCE",     "Reliance Industries Ltd",                     "Energy"),
    ("SBILIFE",      "SBI Life Insurance Company Ltd",              "Financials"),
    ("SBIN",         "State Bank of India",                         "Financials"),
    ("SHRIRAMFIN",   "Shriram Finance Ltd",                         "Financials"),
    ("SUNPHARMA",    "Sun Pharmaceutical Industries Ltd",           "Healthcare"),
    ("TATACONSUM",   "Tata Consumer Products Ltd",                  "Consumer Staples"),
    ("TATAMOTORS",   "Tata Motors Ltd",                             "Consumer Discretionary"),
    ("TATASTEEL",    "Tata Steel Ltd",                              "Materials"),
    ("TCS",          "Tata Consultancy Services Ltd",               "Information Technology"),
    ("TECHM",        "Tech Mahindra Ltd",                           "Information Technology"),
    ("TITAN",        "Titan Company Ltd",                           "Consumer Discretionary"),
    ("ULTRACEMCO",   "UltraTech Cement Ltd",                        "Materials"),
    ("WIPRO",        "Wipro Ltd",                                   "Information Technology"),

    # ── Nifty Next 50 ────────────────────────────────────────────────────────
    ("ABB",          "ABB India Ltd",                               "Industrials"),
    ("AMBUJACEM",    "Ambuja Cements Ltd",                          "Materials"),
    ("AUROPHARMA",   "Aurobindo Pharma Ltd",                        "Healthcare"),
    ("BAJAJHLDNG",   "Bajaj Holdings & Investment Ltd",             "Financials"),
    ("BANKBARODA",   "Bank of Baroda",                              "Financials"),
    ("BERGEPAINT",   "Berger Paints India Ltd",                     "Materials"),
    ("BOSCHLTD",     "Bosch Ltd",                                   "Consumer Discretionary"),
    ("CANBK",        "Canara Bank",                                 "Financials"),
    ("CHOLAFIN",     "Cholamandalam Investment and Finance Co Ltd", "Financials"),
    ("COLPAL",       "Colgate-Palmolive (India) Ltd",               "Consumer Staples"),
    ("CUMMINSIND",   "Cummins India Ltd",                           "Industrials"),
    ("DLF",          "DLF Ltd",                                     "Real Estate"),
    ("GODREJCP",     "Godrej Consumer Products Ltd",                "Consumer Staples"),
    ("GODREJPROP",   "Godrej Properties Ltd",                       "Real Estate"),
    ("HAVELLS",      "Havells India Ltd",                           "Industrials"),
    ("HINDZINC",     "Hindustan Zinc Ltd",                          "Materials"),
    ("ICICIGI",      "ICICI Lombard General Insurance Co Ltd",      "Financials"),
    ("ICICIPRULI",   "ICICI Prudential Life Insurance Co Ltd",      "Financials"),
    ("IDFCFIRSTB",   "IDFC First Bank Ltd",                         "Financials"),
    ("IGL",          "Indraprastha Gas Ltd",                        "Utilities"),
    ("INDUSTOWER",   "Indus Towers Ltd",                            "Communication Services"),
    ("IOC",          "Indian Oil Corporation Ltd",                  "Energy"),
    ("IRCTC",        "Indian Railway Catering and Tourism Corp",    "Industrials"),
    ("LUPIN",        "Lupin Ltd",                                   "Healthcare"),
    ("MARICO",       "Marico Ltd",                                  "Consumer Staples"),
    ("MCDOWELL-N",   "United Spirits Ltd",                          "Consumer Staples"),
    ("MUTHOOTFIN",   "Muthoot Finance Ltd",                         "Financials"),
    ("NAUKRI",       "Info Edge (India) Ltd",                       "Communication Services"),
    ("NHPC",         "NHPC Ltd",                                    "Utilities"),
    ("NMDC",         "NMDC Ltd",                                    "Materials"),
    ("OFSS",         "Oracle Financial Services Software Ltd",      "Information Technology"),
    ("PAGEIND",      "Page Industries Ltd",                         "Consumer Discretionary"),
    ("PIDILITIND",   "Pidilite Industries Ltd",                     "Materials"),
    ("PIIND",        "PI Industries Ltd",                           "Materials"),
    ("PNB",          "Punjab National Bank",                        "Financials"),
    ("RECLTD",       "REC Ltd",                                     "Financials"),
    ("SIEMENS",      "Siemens Ltd",                                 "Industrials"),
    ("TRENT",        "Trent Ltd",                                   "Consumer Discretionary"),
    ("TORNTPHARM",   "Torrent Pharmaceuticals Ltd",                 "Healthcare"),
    ("TVSMOTOR",     "TVS Motor Company Ltd",                       "Consumer Discretionary"),
    ("UNIONBANK",    "Union Bank of India",                         "Financials"),
    ("UPL",          "UPL Ltd",                                     "Materials"),
    ("VEDL",         "Vedanta Ltd",                                 "Materials"),
    ("VOLTAS",       "Voltas Ltd",                                  "Industrials"),
    ("ZYDUSLIFE",    "Zydus Lifesciences Ltd",                      "Healthcare"),

    # ── Nifty Midcap 150 ─────────────────────────────────────────────────────
    ("AARTIIND",     "Aarti Industries Ltd",                        "Materials"),
    ("ABBOTINDIA",   "Abbott India Ltd",                            "Healthcare"),
    ("ABCAPITAL",    "Aditya Birla Capital Ltd",                    "Financials"),
    ("ABFRL",        "Aditya Birla Fashion and Retail Ltd",         "Consumer Discretionary"),
    ("ACC",          "ACC Ltd",                                     "Materials"),
    ("ADANIGREEN",   "Adani Green Energy Ltd",                      "Utilities"),
    ("ADANIPOWER",   "Adani Power Ltd",                             "Utilities"),
    ("ADANITRANS",   "Adani Transmission Ltd",                      "Utilities"),
    ("ALKEM",        "Alkem Laboratories Ltd",                      "Healthcare"),
    ("APLLTD",       "Alembic Pharmaceuticals Ltd",                 "Healthcare"),
    ("ASTRAL",       "Astral Ltd",                                  "Industrials"),
    ("ATUL",         "Atul Ltd",                                    "Materials"),
    ("AUBANK",       "AU Small Finance Bank Ltd",                   "Financials"),
    ("BALKRISIND",   "Balkrishna Industries Ltd",                   "Consumer Discretionary"),
    ("BANDHANBNK",   "Bandhan Bank Ltd",                            "Financials"),
    ("BATAINDIA",    "Bata India Ltd",                              "Consumer Discretionary"),
    ("BEL",          "Bharat Electronics Ltd",                      "Industrials"),
    ("BHARATFORG",   "Bharat Forge Ltd",                            "Industrials"),
    ("BHEL",         "Bharat Heavy Electricals Ltd",                "Industrials"),
    ("BIOCON",       "Biocon Ltd",                                  "Healthcare"),
    ("BLUEDART",     "Blue Dart Express Ltd",                       "Industrials"),
    ("BSOFT",        "Birlasoft Ltd",                               "Information Technology"),
    ("CANFINHOME",   "Can Fin Homes Ltd",                           "Financials"),
    ("CASTROLIND",   "Castrol India Ltd",                           "Energy"),
    ("CESC",         "CESC Ltd",                                    "Utilities"),
    ("CGPOWER",      "CG Power and Industrial Solutions Ltd",       "Industrials"),
    ("CHAMBLFERT",   "Chambal Fertilisers and Chemicals Ltd",       "Materials"),
    ("COFORGE",      "Coforge Ltd",                                 "Information Technology"),
    ("CONCOR",       "Container Corporation of India Ltd",          "Industrials"),
    ("CROMPTON",     "Crompton Greaves Consumer Electricals Ltd",   "Industrials"),
    ("CUB",          "City Union Bank Ltd",                         "Financials"),
    ("CYIENT",       "Cyient Ltd",                                  "Information Technology"),
    ("DALBHARAT",    "Dalmia Bharat Ltd",                           "Materials"),
    ("DAUPHINE",     "Dauphine Ltd",                                "Industrials"),
    ("DEEPAKNTR",    "Deepak Nitrite Ltd",                          "Materials"),
    ("DELTACORP",    "Delta Corp Ltd",                              "Consumer Discretionary"),
    ("DIXON",        "Dixon Technologies (India) Ltd",              "Information Technology"),
    ("DMART",        "Avenue Supermarts Ltd",                       "Consumer Staples"),
    ("EMAMILTD",     "Emami Ltd",                                   "Consumer Staples"),
    ("ENDURANCE",    "Endurance Technologies Ltd",                  "Consumer Discretionary"),
    ("ENGINERSIN",   "Engineers India Ltd",                         "Industrials"),
    ("EQUITASBNK",   "Equitas Small Finance Bank Ltd",              "Financials"),
    ("ESCORTS",      "Escorts Kubota Ltd",                          "Consumer Discretionary"),
    ("EXIDEIND",     "Exide Industries Ltd",                        "Consumer Discretionary"),
    ("FACT",         "Fertilisers and Chemicals Travancore Ltd",    "Materials"),
    ("FEDERALBNK",   "Federal Bank Ltd",                            "Financials"),
    ("FLUOROCHEM",   "Gujarat Fluorochemicals Ltd",                 "Materials"),
    ("FORTIS",       "Fortis Healthcare Ltd",                       "Healthcare"),
    ("GAIL",         "GAIL (India) Ltd",                            "Utilities"),
    ("GLENMARK",     "Glenmark Pharmaceuticals Ltd",                "Healthcare"),
    ("GMRINFRA",     "GMR Airports Infrastructure Ltd",             "Industrials"),
    ("GNFC",         "Gujarat Narmada Valley Fertilizers Co Ltd",   "Materials"),
    ("GODFRYPHLP",   "Godfrey Phillips India Ltd",                  "Consumer Staples"),
    ("GODREJIND",    "Godrej Industries Ltd",                       "Conglomerate"),
    ("GRANULES",     "Granules India Ltd",                          "Healthcare"),
    ("GSPL",         "Gujarat State Petronet Ltd",                  "Utilities"),
    ("HFCL",         "HFCL Ltd",                                    "Communication Services"),
    ("HONAUT",       "Honeywell Automation India Ltd",              "Industrials"),
    ("HUDCO",        "Housing and Urban Development Corp Ltd",      "Financials"),
    ("IDBI",         "IDBI Bank Ltd",                               "Financials"),
    ("IGAS",         "Indraprastha Gas Ltd",                        "Utilities"),
    ("INDIANB",      "Indian Bank",                                 "Financials"),
    ("INDIAMART",    "IndiaMART InterMESH Ltd",                     "Communication Services"),
    ("INDIGOPNTS",   "Indigo Paints Ltd",                           "Materials"),
    ("INTELLECT",    "Intellect Design Arena Ltd",                  "Information Technology"),
    ("IPCALAB",      "IPCA Laboratories Ltd",                       "Healthcare"),
    ("IRB",          "IRB Infrastructure Developers Ltd",           "Industrials"),
    ("IRFC",         "Indian Railway Finance Corporation Ltd",      "Financials"),
    ("ISEC",         "ICICI Securities Ltd",                        "Financials"),
    ("JINDALSTEL",   "Jindal Steel & Power Ltd",                    "Materials"),
    ("JKCEMENT",     "JK Cement Ltd",                               "Materials"),
    ("JSWENERGY",    "JSW Energy Ltd",                              "Utilities"),
    ("JUBILANT",     "Jubilant Ingrevia Ltd",                       "Materials"),
    ("JUBLFOOD",     "Jubilant Foodworks Ltd",                      "Consumer Discretionary"),
    ("KAJARIACER",   "Kajaria Ceramics Ltd",                        "Industrials"),
    ("KALPATPOWR",   "Kalpataru Projects International Ltd",        "Industrials"),
    ("KANSAINER",    "Kansai Nerolac Paints Ltd",                   "Materials"),
    ("KARURVYSYA",   "Karur Vysya Bank Ltd",                        "Financials"),
    ("KEC",          "KEC International Ltd",                       "Industrials"),
    ("KPITTECH",     "KPIT Technologies Ltd",                       "Information Technology"),
    ("KRISHNADEF",   "Krishna Defence and Allied Industries Ltd",   "Industrials"),
    ("LALPATHLAB",   "Dr. Lal Path Labs Ltd",                       "Healthcare"),
    ("LAURUSLABS",   "Laurus Labs Ltd",                             "Healthcare"),
    ("LICI",         "Life Insurance Corporation of India",         "Financials"),
    ("LINDEINDIA",   "Linde India Ltd",                             "Materials"),
    ("LTIM",         "LTIMindtree Ltd",                             "Information Technology"),
    ("LTTS",         "L&T Technology Services Ltd",                 "Information Technology"),
    ("MAHINDCIE",    "Mahindra CIE Automotive Ltd",                 "Consumer Discretionary"),
    ("MAPMYINDIA",   "C.E. Info Systems Ltd",                       "Information Technology"),
    ("MAXHEALTH",    "Max Healthcare Institute Ltd",                "Healthcare"),
    ("MFSL",         "Max Financial Services Ltd",                  "Financials"),
    ("MMFINANCE",    "Mahindra & Mahindra Financial Services Ltd",  "Financials"),
    ("MOTHERSON",    "Samvardhana Motherson International Ltd",     "Consumer Discretionary"),
    ("MPHASIS",      "Mphasis Ltd",                                 "Information Technology"),
    ("MRF",          "MRF Ltd",                                     "Consumer Discretionary"),
    ("NAVINFLUOR",   "Navin Fluorine International Ltd",            "Materials"),
    ("NIACL",        "New India Assurance Co Ltd",                  "Financials"),
    ("NLCINDIA",     "NLC India Ltd",                               "Utilities"),
    ("OBEROIRLTY",   "Oberoi Realty Ltd",                           "Real Estate"),
    ("OIL",          "Oil India Ltd",                               "Energy"),
    ("OLDTRIBUNE",   "Tribune Press Ltd",                           "Communication Services"),
    ("PATANJALI",    "Patanjali Foods Ltd",                         "Consumer Staples"),
    ("PERSISTENT",   "Persistent Systems Ltd",                      "Information Technology"),
    ("PFC",          "Power Finance Corporation Ltd",               "Financials"),
    ("PFIZER",       "Pfizer Ltd",                                  "Healthcare"),
    ("PHOENIXLTD",   "The Phoenix Mills Ltd",                       "Real Estate"),
    ("POLYCAB",      "Polycab India Ltd",                           "Industrials"),
    ("PRESTIGE",     "Prestige Estates Projects Ltd",               "Real Estate"),
    ("PRINCEPIPE",   "Prince Pipes and Fittings Ltd",               "Industrials"),
    ("PNBHOUSING",   "PNB Housing Finance Ltd",                     "Financials"),
    ("QUESS",        "Quess Corp Ltd",                              "Industrials"),
    ("RADICO",       "Radico Khaitan Ltd",                          "Consumer Staples"),
    ("RAILTEL",      "RailTel Corporation of India Ltd",            "Communication Services"),
    ("RAJESHEXPO",   "Rajesh Exports Ltd",                          "Consumer Discretionary"),
    ("RAMCOCEM",     "The Ramco Cements Ltd",                       "Materials"),
    ("RBLBANK",      "RBL Bank Ltd",                                "Financials"),
    ("REDINGTON",    "Redington Ltd",                               "Information Technology"),
    ("ROUTE",        "Route Mobile Ltd",                            "Communication Services"),
    ("SAIL",         "Steel Authority of India Ltd",                "Materials"),
    ("SBICARD",      "SBI Cards and Payment Services Ltd",          "Financials"),
    ("SCHAEFFLER",   "Schaeffler India Ltd",                        "Industrials"),
    ("SHYAMMETL",    "Shyam Metalics and Energy Ltd",               "Materials"),
    ("SKFINDIA",     "SKF India Ltd",                               "Industrials"),
    ("SOBHA",        "Sobha Ltd",                                   "Real Estate"),
    ("SOLARINDS",    "Solar Industries India Ltd",                  "Industrials"),
    ("SONACOMS",     "Sona BLW Precision Forgings Ltd",             "Consumer Discretionary"),
    ("STAR",         "Star Health and Allied Insurance Co Ltd",     "Financials"),
    ("STARCEMENT",   "Star Cement Ltd",                             "Materials"),
    ("STLTECH",      "Sterlite Technologies Ltd",                   "Communication Services"),
    ("SUMICHEM",     "Sumitomo Chemical India Ltd",                 "Materials"),
    ("SUNPHARMA",    "Sun Pharmaceutical Industries Ltd",           "Healthcare"),  # already in N50
    ("SUNTV",        "Sun TV Network Ltd",                          "Communication Services"),
    ("SUPREMEIND",   "Supreme Industries Ltd",                      "Industrials"),
    ("SUVENPHAR",    "Suven Pharmaceuticals Ltd",                   "Healthcare"),
    ("SUZLON",       "Suzlon Energy Ltd",                           "Utilities"),
    ("TANLA",        "Tanla Platforms Ltd",                         "Communication Services"),
    ("TATACOMM",     "Tata Communications Ltd",                     "Communication Services"),
    ("TATAELXSI",    "Tata Elxsi Ltd",                              "Information Technology"),
    ("TATAINVEST",   "Tata Investment Corporation Ltd",             "Financials"),
    ("TATAMTRDVR",   "Tata Motors Ltd DVR",                         "Consumer Discretionary"),
    ("TATAGLOBEV",   "Tata Global Beverages Ltd",                   "Consumer Staples"),
    ("THERMAX",      "Thermax Ltd",                                 "Industrials"),
    ("TIMKEN",       "Timken India Ltd",                            "Industrials"),
    ("TORNTPOWER",   "Torrent Power Ltd",                           "Utilities"),
    ("TRIDENT",      "Trident Ltd",                                 "Consumer Discretionary"),
    ("TTKPRESTIG",   "TTK Prestige Ltd",                            "Consumer Discretionary"),
    ("UCOBANK",      "UCO Bank",                                    "Financials"),
    ("UJJIVAN",      "Ujjivan Financial Services Ltd",              "Financials"),
    ("UJJIVANSFB",   "Ujjivan Small Finance Bank Ltd",              "Financials"),
    ("UNOMINDA",     "UNO Minda Ltd",                               "Consumer Discretionary"),
    ("VGUARD",       "V-Guard Industries Ltd",                      "Industrials"),
    ("VBL",          "Varun Beverages Ltd",                         "Consumer Staples"),
    ("VINATIORGA",   "Vinati Organics Ltd",                         "Materials"),
    ("VIPIND",       "VIP Industries Ltd",                          "Consumer Discretionary"),
    ("WELCORP",      "Welspun Corp Ltd",                            "Industrials"),
    ("WELSPUNLIV",   "Welspun Living Ltd",                          "Consumer Discretionary"),
    ("WESTLIFE",     "Westlife Foodworld Ltd",                      "Consumer Discretionary"),
    ("WHIRLPOOL",    "Whirlpool of India Ltd",                      "Consumer Discretionary"),
    ("WIPRO",        "Wipro Ltd",                                   "Information Technology"),   # already in N50
    ("WOCKPHARMA",   "Wockhardt Ltd",                               "Healthcare"),
    ("YESBANK",      "Yes Bank Ltd",                                "Financials"),
    ("ZEEL",         "Zee Entertainment Enterprises Ltd",           "Communication Services"),
    ("ZENSARTECH",   "Zensar Technologies Ltd",                     "Information Technology"),
    ("ZOMATO",       "Zomato Ltd",                                  "Consumer Discretionary"),

    # ── Nifty Smallcap (liquid subset) ───────────────────────────────────────
    ("AARTIDRUGS",   "Aarti Drugs Ltd",                             "Healthcare"),
    ("AAVAS",        "AAVAS Financiers Ltd",                        "Financials"),
    ("ACCELYA",      "Accelya Solutions India Ltd",                 "Information Technology"),
    ("ACE",          "Action Construction Equipment Ltd",           "Industrials"),
    ("AFFLE",        "Affle (India) Ltd",                           "Communication Services"),
    ("AJANTPHARM",   "Ajanta Pharma Ltd",                           "Healthcare"),
    ("ALEMBICLTD",   "Alembic Ltd",                                 "Healthcare"),
    ("AMARAJABAT",   "Amara Raja Energy & Mobility Ltd",            "Consumer Discretionary"),
    ("ANANTRAJ",     "Anant Raj Ltd",                               "Real Estate"),
    ("ANGELONE",     "Angel One Ltd",                               "Financials"),
    ("APTUS",        "Aptus Value Housing Finance India Ltd",       "Financials"),
    ("ARVINDFASN",   "Arvind Fashions Ltd",                         "Consumer Discretionary"),
    ("ASAHIINDIA",   "Asahi India Glass Ltd",                       "Materials"),
    ("ASHOKLEY",     "Ashok Leyland Ltd",                           "Consumer Discretionary"),
    ("ATUL",         "Atul Ltd",                                    "Materials"),
    ("AWHCL",        "Amritsar Wine House Company Ltd",             "Consumer Staples"),
    ("BALRAMCHIN",   "Balrampur Chini Mills Ltd",                   "Consumer Staples"),
    ("BAYERCROP",    "Bayer CropScience Ltd",                       "Materials"),
    ("BEML",         "BEML Ltd",                                    "Industrials"),
    ("BIGBLOC",      "Bigbloc Construction Ltd",                    "Materials"),
    ("BRIGADE",      "Brigade Enterprises Ltd",                     "Real Estate"),
    ("BSE",          "BSE Ltd",                                     "Financials"),
    ("BSOFT",        "Birlasoft Ltd",                               "Information Technology"),
    ("CAPLIPOINT",   "Caplin Point Laboratories Ltd",               "Healthcare"),
    ("CARERATING",   "CARE Ratings Ltd",                            "Financials"),
    ("CDSL",         "Central Depository Services (India) Ltd",     "Financials"),
    ("CEATLTD",      "CEAT Ltd",                                    "Consumer Discretionary"),
    ("CENTURYPLY",   "Century Plyboards (India) Ltd",               "Industrials"),
    ("CENTURYTEX",   "Century Textiles & Industries Ltd",           "Consumer Discretionary"),
    ("CERA",         "Cera Sanitaryware Ltd",                       "Industrials"),
    ("CHALET",       "Chalet Hotels Ltd",                           "Consumer Discretionary"),
    ("CLEAN",        "Clean Science and Technology Ltd",            "Materials"),
    ("CRAFTSMAN",    "Craftsman Automation Ltd",                    "Industrials"),
    ("DCMSHRIRAM",   "DCM Shriram Ltd",                             "Materials"),
    ("DELHIVERY",    "Delhivery Ltd",                               "Industrials"),
    ("DHANI",        "Dhani Services Ltd",                          "Financials"),
    ("DHANUKA",      "Dhanuka Agritech Ltd",                        "Materials"),
    ("EIL",          "Engineers India Ltd",                         "Industrials"),
    ("EIDPARRY",     "EID Parry (India) Ltd",                       "Consumer Staples"),
    ("ELECON",       "Elecon Engineering Company Ltd",              "Industrials"),
    ("ELGIEQUIP",    "Elgi Equipments Ltd",                         "Industrials"),
    ("EPIGRAL",      "Epigral Ltd",                                 "Materials"),
    ("FINEORG",      "Fine Organic Industries Ltd",                 "Materials"),
    ("FINPIPE",      "Finolex Industries Ltd",                      "Industrials"),
    ("FINOLEX",      "Finolex Cables Ltd",                          "Industrials"),
    ("FLAIR",        "Flair Writing Industries Ltd",                "Consumer Discretionary"),
    ("GABRIEL",      "Gabriel India Ltd",                           "Consumer Discretionary"),
    ("GALAXYSURF",   "Galaxy Surfactants Ltd",                      "Materials"),
    ("GARWARE",      "Garware Hi-Tech Films Ltd",                   "Materials"),
    ("GENESYS",      "Genesys International Corporation Ltd",       "Information Technology"),
    ("GESHIP",       "The Great Eastern Shipping Co Ltd",           "Industrials"),
    ("GODREJAGRO",   "Godrej Agrovet Ltd",                          "Consumer Staples"),
    ("GPIL",         "Godawari Power and Ispat Ltd",                "Materials"),
    ("GREENPLY",     "Greenply Industries Ltd",                     "Industrials"),
    ("GRINDWELL",    "Grindwell Norton Ltd",                        "Industrials"),
    ("GUJGASLTD",    "Gujarat Gas Ltd",                             "Utilities"),
    ("GULFOILLUB",   "Gulf Oil Lubricants India Ltd",               "Energy"),
    ("HAPPSTMNDS",   "Happiest Minds Technologies Ltd",             "Information Technology"),
    ("HARSHA",       "Harsha Engineers International Ltd",          "Industrials"),
    ("HEG",          "HEG Ltd",                                     "Materials"),
    ("HERANBA",      "Heranba Industries Ltd",                      "Materials"),
    ("HGINFRA",      "H.G. Infra Engineering Ltd",                  "Industrials"),
    ("HIKAL",        "Hikal Ltd",                                   "Materials"),
    ("HLEGLAS",      "HLE Glascoat Ltd",                            "Industrials"),
    ("HMVL",         "Hindustan Media Ventures Ltd",                "Communication Services"),
    ("HOMEFIRST",    "Home First Finance Company India Ltd",        "Financials"),
    ("IDEAFORGE",    "ideaForge Technology Ltd",                    "Industrials"),
    ("IFBIND",       "IFB Industries Ltd",                          "Consumer Discretionary"),
    ("IGARASHI",     "Igarashi Motors India Ltd",                   "Industrials"),
    ("INDIASHLTR",   "India Shelter Finance Corporation Ltd",       "Financials"),
    ("INDIGRID",     "IndiGrid Infrastructure Investment Trust",    "Utilities"),
    ("INDOCO",       "Indoco Remedies Ltd",                         "Healthcare"),
    ("INDOSTAR",     "IndoStar Capital Finance Ltd",                "Financials"),
    ("INOXWIND",     "Inox Wind Ltd",                               "Utilities"),
    ("INSECTICIDE",  "Insecticides (India) Ltd",                    "Materials"),
    ("IOB",          "Indian Overseas Bank",                        "Financials"),
    ("ISGEC",        "Isgec Heavy Engineering Ltd",                 "Industrials"),
    ("ITDCEM",       "ITD Cementation India Ltd",                   "Industrials"),
    ("JBCHEPHARM",   "J.B. Chemicals & Pharmaceuticals Ltd",        "Healthcare"),
    ("JBMA",         "JBM Auto Ltd",                                "Consumer Discretionary"),
    ("JINDALPOLY",   "Jindal Poly Films Ltd",                       "Materials"),
    ("JKIL",         "J Kumar Infraprojects Ltd",                   "Industrials"),
    ("JKLAKSHMI",    "JK Lakshmi Cement Ltd",                       "Materials"),
    ("JMFINANCIL",   "JM Financial Ltd",                            "Financials"),
    ("JPPOWER",      "Jaiprakash Power Ventures Ltd",               "Utilities"),
    ("JSPL",         "Jindal Steel & Power Ltd",                    "Materials"),
    ("JUSTDIAL",     "Just Dial Ltd",                               "Communication Services"),
    ("JYOTHYLAB",    "Jyothy Labs Ltd",                             "Consumer Staples"),
    ("KFINTECH",     "KFin Technologies Ltd",                       "Information Technology"),
    ("KNRCON",       "KNR Constructions Ltd",                       "Industrials"),
    ("KOLTEPATIL",   "Kolte-Patil Developers Ltd",                  "Real Estate"),
    ("KPIL",         "Kalpataru Projects International Ltd",        "Industrials"),
    ("KRBL",         "KRBL Ltd",                                    "Consumer Staples"),
    ("KSB",          "KSB Ltd",                                     "Industrials"),
    ("LATENTVIEW",   "LatentView Analytics Ltd",                    "Information Technology"),
    ("LEMONTREE",    "Lemon Tree Hotels Ltd",                       "Consumer Discretionary"),
    ("LICENSEE",     "Licence Ltd",                                 "Consumer Staples"),
    ("LLOYDSME",     "Lloyds Metals and Energy Ltd",                "Materials"),
    ("LXCHEM",       "Laxmi Organic Industries Ltd",                "Materials"),
    ("MAHSEAMLES",   "Maharashtra Seamless Ltd",                    "Materials"),
    ("MANAPPURAM",   "Manappuram Finance Ltd",                      "Financials"),
    ("MASFIN",       "MAS Financial Services Ltd",                  "Financials"),
    ("MASTEK",       "Mastek Ltd",                                  "Information Technology"),
    ("MEDANTA",      "Global Health Ltd",                           "Healthcare"),
    ("METROPOLIS",   "Metropolis Healthcare Ltd",                   "Healthcare"),
    ("MHRIL",        "Mahindra Holidays & Resorts India Ltd",       "Consumer Discretionary"),
    ("MINDACORP",    "Minda Corporation Ltd",                       "Consumer Discretionary"),
    ("MNRE",         "Montecarlo Ltd",                              "Industrials"),
    ("MOIL",         "MOIL Ltd",                                    "Materials"),
    ("MOTILALOFS",   "Motilal Oswal Financial Services Ltd",        "Financials"),
    ("MTAR",         "MTAR Technologies Ltd",                       "Industrials"),
    ("NATCOPHARM",   "Natco Pharma Ltd",                            "Healthcare"),
    ("NBCC",         "NBCC (India) Ltd",                            "Industrials"),
    ("NESCO",        "Nesco Ltd",                                   "Real Estate"),
    ("NETWORK18",    "Network18 Media & Investments Ltd",           "Communication Services"),
    ("NOCIL",        "NOCIL Ltd",                                   "Materials"),
    ("NUVOCO",       "Nuvoco Vistas Corporation Ltd",               "Materials"),
    ("OLECTRA",      "Olectra Greentech Ltd",                       "Industrials"),
    ("ONWARDTEC",    "Onward Technologies Ltd",                     "Information Technology"),
    ("ORIENTELEC",   "Orient Electric Ltd",                         "Industrials"),
    ("ORIENTCEM",    "Orient Cement Ltd",                           "Materials"),
    ("PATELENG",     "Patel Engineering Ltd",                       "Industrials"),
    ("PCBL",         "PCBL Ltd",                                    "Materials"),
    ("PDSL",         "PDS Ltd",                                     "Consumer Discretionary"),
    ("PGIL",         "Pearl Global Industries Ltd",                 "Consumer Discretionary"),
    ("PGHH",         "Procter & Gamble Hygiene and Health Care Ltd","Consumer Staples"),
    ("PNBGILTS",     "PNB Gilts Ltd",                               "Financials"),
    ("POONAWALLA",   "Poonawalla Fincorp Ltd",                      "Financials"),
    ("POWERINDIA",   "Hitachi Energy India Ltd",                    "Industrials"),
    ("PPAP",         "PPAP Automotive Ltd",                         "Consumer Discretionary"),
    ("PRAJIND",      "Praj Industries Ltd",                         "Industrials"),
    ("PRICOLLTD",    "Pricol Ltd",                                  "Consumer Discretionary"),
    ("PRIME",        "Prime Focus Ltd",                             "Communication Services"),
    ("PRINCEPIPE",   "Prince Pipes and Fittings Ltd",               "Industrials"),
    ("PRSMJOHNSN",   "Prism Johnson Ltd",                           "Materials"),
    ("PRUDENT",      "Prudent Corporate Advisory Services Ltd",     "Financials"),
    ("PSB",          "Punjab & Sind Bank",                          "Financials"),
    ("PVRINOX",      "PVR INOX Ltd",                                "Consumer Discretionary"),
    ("RAJRATAN",     "Rajratan Global Wire Ltd",                    "Industrials"),
    ("RAMKY",        "Ramky Infrastructure Ltd",                    "Industrials"),
    ("RATNAMANI",    "Ratnamani Metals and Tubes Ltd",               "Industrials"),
    ("RKFORGE",      "Ramkrishna Forgings Ltd",                     "Industrials"),
    ("RPGLIFE",      "RPG Life Sciences Ltd",                       "Healthcare"),
    ("RPOWER",       "Reliance Power Ltd",                          "Utilities"),
    ("RSYSTEMS",     "R Systems International Ltd",                 "Information Technology"),
    ("SAFARI",       "Safari Industries (India) Ltd",               "Consumer Discretionary"),
    ("SAKSOFT",      "Saksoft Ltd",                                 "Information Technology"),
    ("SAREGAMA",     "Saregama India Ltd",                          "Communication Services"),
    ("SATIN",        "Satin Creditcare Network Ltd",                "Financials"),
    ("SBCL",         "Shivalik Bimetal Controls Ltd",               "Industrials"),
    ("SBICARD",      "SBI Cards and Payment Services Ltd",          "Financials"),
    ("SEQUENT",      "SeQuent Scientific Ltd",                      "Healthcare"),
    ("SHARDACROP",   "Sharda Cropchem Ltd",                         "Materials"),
    ("SHOPERSTOP",   "Shoppers Stop Ltd",                           "Consumer Discretionary"),
    ("SHREDIGCEM",   "Shree Digvijay Cement Co Ltd",                "Materials"),
    ("SHREECEM",     "Shree Cement Ltd",                            "Materials"),
    ("SIGNATURE",    "Signature Global (India) Ltd",                "Real Estate"),
    ("SJVN",         "SJVN Ltd",                                    "Utilities"),
    ("SMSPHARMA",    "SMS Pharmaceuticals Ltd",                     "Healthcare"),
    ("SNOWMAN",      "Snowman Logistics Ltd",                       "Industrials"),
    ("SOLARA",       "Solara Active Pharma Sciences Ltd",           "Healthcare"),
    ("SPARC",        "Sun Pharma Advanced Research Company Ltd",    "Healthcare"),
    ("SPENCERS",     "Spencer's Retail Ltd",                        "Consumer Staples"),
    ("SPLPETRO",     "SEPC Ltd",                                    "Energy"),
    ("SPMLINFRA",    "SPML Infra Ltd",                              "Industrials"),
    ("SRHHYPOLTD",   "Sree Rayalaseema Hi-Strength Hypo Ltd",       "Materials"),
    ("SRTRANSFIN",   "Shriram Transport Finance Co Ltd",            "Financials"),
    ("STLTECH",      "Sterlite Technologies Ltd",                   "Communication Services"),
    ("SUBCAPCITY",   "Subros Ltd",                                  "Consumer Discretionary"),
    ("SUDARSCHEM",   "Sudarshan Chemical Industries Ltd",           "Materials"),
    ("SUNFLAG",      "Sunflag Iron and Steel Co Ltd",               "Materials"),
    ("SUNDRMFAST",   "Sundram Fasteners Ltd",                       "Industrials"),
    ("SUNTECK",      "Sunteck Realty Ltd",                          "Real Estate"),
    ("SURYAROSNI",   "Surya Roshni Ltd",                            "Industrials"),
    ("SVLL",         "Shree Vasu Logistics Ltd",                    "Industrials"),
    ("SWSOLAR",      "Sterling and Wilson Renewable Energy Ltd",    "Utilities"),
    ("SYMPHONY",     "Symphony Ltd",                                "Consumer Discretionary"),
    ("SYNGENE",      "Syngene International Ltd",                   "Healthcare"),
    ("TAKE",         "Take Solutions Ltd",                          "Information Technology"),
    ("TASTYBITE",    "Tasty Bite Eatables Ltd",                     "Consumer Staples"),
    ("TATACHEM",     "Tata Chemicals Ltd",                          "Materials"),
    ("TATAPOWER",    "Tata Power Company Ltd",                      "Utilities"),
    ("TATASTLBSL",   "Tata Steel BSL Ltd",                          "Materials"),
    ("TCNSBRANDS",   "TCNS Clothing Co Ltd",                        "Consumer Discretionary"),
    ("TEAMLEASE",    "TeamLease Services Ltd",                      "Industrials"),
    ("THYROCARE",    "Thyrocare Technologies Ltd",                  "Healthcare"),
    ("TIINDIA",      "Tube Investments of India Ltd",               "Industrials"),
    ("TIMETECHNO",   "Time Technoplast Ltd",                        "Industrials"),
    ("TINPLATE",     "The Tinplate Company of India Ltd",           "Materials"),
    ("TIPSFILMS",    "Tips Films Ltd",                              "Communication Services"),
    ("TITAGARH",     "Titagarh Rail Systems Ltd",                   "Industrials"),
    ("TORNTPOWER",   "Torrent Power Ltd",                           "Utilities"),
    ("TPLPLASTEH",   "TPL Plastech Ltd",                            "Industrials"),
    ("TREJHARA",     "TREJHARA SOLUTIONS LTD",                      "Information Technology"),
    ("TRITURBINE",   "Triveni Turbine Ltd",                         "Industrials"),
    ("TTML",         "Tata Teleservices (Maharashtra) Ltd",         "Communication Services"),
    ("TV18BRDCST",   "TV18 Broadcast Ltd",                          "Communication Services"),
    ("TVTODAY",      "TV Today Network Ltd",                        "Communication Services"),
    ("TVSMOTOR",     "TVS Motor Company Ltd",                       "Consumer Discretionary"),
    ("UNIPARTS",     "Uniparts India Ltd",                          "Industrials"),
    ("UTIAMC",       "UTI Asset Management Company Ltd",            "Financials"),
    ("VAIBHAVGBL",   "Vaibhav Global Ltd",                          "Consumer Discretionary"),
    ("VARROC",       "Varroc Engineering Ltd",                      "Consumer Discretionary"),
    ("VEDL",         "Vedanta Ltd",                                 "Materials"),
    ("VENKEYS",      "Venky's (India) Ltd",                         "Consumer Staples"),
    ("VESUVIUS",     "Vesuvius India Ltd",                          "Industrials"),
    ("VIPIND",       "VIP Industries Ltd",                          "Consumer Discretionary"),
    ("VIRINCHI",     "Virinchi Ltd",                                "Information Technology"),
    ("VISCOIND",     "Viscose Industex Ltd",                        "Consumer Discretionary"),
    ("VMART",        "V-Mart Retail Ltd",                           "Consumer Discretionary"),
    ("VSTIND",       "VST Industries Ltd",                          "Consumer Staples"),
    ("WABAG",        "VA Tech Wabag Ltd",                           "Industrials"),
    ("WABCOINDIA",   "Wabco India Ltd",                             "Consumer Discretionary"),
    ("WELSPUNLIV",   "Welspun Living Ltd",                          "Consumer Discretionary"),
    ("WINDMACHINES", "Windworld (India) Ltd",                       "Utilities"),
    ("WONDERLA",     "Wonderla Holidays Ltd",                       "Consumer Discretionary"),
    ("XCHANGING",    "Xchanging Solutions Ltd",                     "Information Technology"),
    ("ZARENERGY",    "Zar Energy Ltd",                              "Utilities"),
    ("ZEEL",         "Zee Entertainment Enterprises Ltd",           "Communication Services"),
    ("ZENTEC",       "Zen Technologies Ltd",                        "Industrials"),
    ("ZFCVINDIA",    "ZF Commercial Vehicle Control Systems Ltd",   "Consumer Discretionary"),
    ("ZIMLAB",       "Zim Laboratories Ltd",                        "Healthcare"),
    ("ZODJRDMKJ",    "Zodiac JRD-MKJ Ltd",                         "Consumer Discretionary"),

    # ── Additional sector stocks (defense, energy, sugar, shipbuilding) ─────────
    ("HAL",          "Hindustan Aeronautics Ltd",                   "Industrials"),
    ("COCHINSHIP",   "Cochin Shipyard Ltd",                         "Industrials"),
    ("MAZDOCK",      "Mazagon Dock Shipbuilders Ltd",               "Industrials"),
    ("MIDHANI",      "Mishra Dhatu Nigam Ltd",                      "Industrials"),
    ("HPCL",         "Hindustan Petroleum Corporation Ltd",         "Energy"),
    ("MRPL",         "Mangalore Refinery and Petrochemicals Ltd",   "Energy"),
    ("PETRONET",     "Petronet LNG Ltd",                            "Energy"),
    ("IREDA",        "Indian Renewable Energy Development Agency",  "Financials"),
    ("TRIVENI",      "Triveni Engineering and Industries Ltd",      "Consumer Staples"),
    ("DHAMPUR",      "Dhampur Sugar Mills Ltd",                     "Consumer Staples"),
    ("EIDPARRY",     "EID Parry (India) Ltd",                       "Consumer Staples"),
    ("RENUKA",       "Shree Renuka Sugars Ltd",                     "Consumer Staples"),
    ("DALALSTREET",  "Dalal Street Investments Ltd",                "Financials"),
    ("BSE",          "BSE Ltd",                                     "Financials"),
    ("CAMS",         "Computer Age Management Services Ltd",        "Financials"),
    ("CDSL",         "Central Depository Services (India) Ltd",     "Financials"),
    ("MCX",          "Multi Commodity Exchange of India Ltd",       "Financials"),
    ("IRFC",         "Indian Railway Finance Corporation Ltd",      "Financials"),
    ("REC",          "REC Ltd",                                     "Financials"),
    ("PFC",          "Power Finance Corporation Ltd",               "Financials"),
    ("NLCINDIA",     "NLC India Ltd",                               "Utilities"),
    ("NHPC",         "NHPC Ltd",                                    "Utilities"),
    ("SJVN",         "SJVN Ltd",                                    "Utilities"),
    ("RVNL",         "Rail Vikas Nigam Ltd",                        "Industrials"),
    ("RAILVIKAS",    "Rail Vikas Nigam Ltd",                        "Industrials"),
    ("IRCON",        "Ircon International Ltd",                     "Industrials"),
    ("NBCC",         "NBCC (India) Ltd",                            "Real Estate"),
    ("HFCL",         "HFCL Ltd",                                    "Information Technology"),
    ("BEL",          "Bharat Electronics Ltd",                      "Industrials"),
    ("BEML",         "BEML Ltd",                                    "Industrials"),
    ("GRSE",         "Garden Reach Shipbuilders and Engineers Ltd", "Industrials"),
    ("MTNL",         "Mahanagar Telephone Nigam Ltd",               "Communication Services"),
    ("NMDC",         "NMDC Ltd",                                    "Materials"),
    ("MOIL",         "MOIL Ltd",                                    "Materials"),
]

# De-duplicate (same symbol can appear in N50 + Midcap etc.)
_seen: set[str] = set()
_deduped: list[tuple[str, str, str]] = []
for _row in NIFTY500:
    if _row[0] not in _seen:
        _seen.add(_row[0])
        _deduped.append(_row)
NIFTY500 = _deduped

# ─── Index instruments ───────────────────────────────────────────────────────
# Tracking only — cannot be traded directly as equities
INDEX_INSTRUMENTS = [
    ("NIFTY 50",      "NSE:NIFTY 50",     256265),
    ("NIFTY BANK",    "NSE:NIFTY BANK",   260105),
    ("INDIA VIX",     "NSE:INDIA VIX",    264969),
    ("NIFTY IT",      "NSE:NIFTY IT",     259849),
    ("NIFTY FMCG",    "NSE:NIFTY FMCG",  257801),
    ("NIFTY MIDCAP",  "NSE:NIFTY MIDCAP", 288009),
]

# ─── Sector → NSE index mapping ─────────────────────────────────────────────
# Maps each sector label (used in NIFTY500 tuples) to its best proxy NSE
# sectoral index available on yfinance. Sectors with no clean proxy are
# omitted — the backtest sector filter simply skips them rather than
# using a noisy or off-topic index.
SECTOR_INDEX_MAP: dict[str, str] = {
    "Financials":              "^NSEBANK",    # Nifty Bank (12 banking stocks)
    "Information Technology":  "^CNXIT",      # Nifty IT (10 IT stocks)
    "Consumer Staples":        "^CNXFMCG",    # Nifty FMCG
    "Healthcare":              "^CNXPHARMA",  # Nifty Pharma
    "Consumer Discretionary":  "^CNXAUTO",    # Nifty Auto (best proxy available)
    "Materials":               "^CNXMETAL",   # Nifty Metal
    "Energy":                  "^CNXENERGY",  # Nifty Energy
    "Real Estate":             "^CNXREALTY",  # Nifty Realty
    # Industrials, Utilities, Communication Services, Conglomerate:
    # no clean NSE sectoral index — sector filter skipped for these.
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_nifty500_symbols() -> list[str]:
    """Return just the trading symbols."""
    return [row[0] for row in NIFTY500]


def get_live_universe() -> list[str]:
    """
    Fetch the full NSE EQ-series equity universe for live/paper trading.

    Downloads NSE's public EQUITY_L.csv and returns all EQ-series symbols
    (excludes BE = trade-to-trade, BZ = suspended/illiquid, rights shares,
    warrants, DVRs etc.).

    Falls back to NIFTY500 if the download fails — ensures the bot always
    starts even without internet access to NSE's website.

    Typical size: ~1700–2200 symbols (EQ-series, normal equities).
    """
    import csv
    import io
    import re

    import requests

    NSE_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
    fallback = get_nifty500_symbols()

    try:
        resp = requests.get(NSE_URL, timeout=15,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        symbols: list[str] = []
        for row in reader:
            sym    = (row.get("SYMBOL") or row.get("Symbol") or "").strip()
            series = (row.get(" SERIES") or row.get("SERIES") or row.get("Series") or "").strip()
            if not sym or series != "EQ":
                continue
            if not re.match(r'^[A-Z0-9&\-]+$', sym):
                continue
            symbols.append(sym)

        if len(symbols) < 100:          # sanity check — partial/corrupt response
            return fallback

        return symbols

    except Exception:
        return fallback


def get_nifty500_by_sector() -> dict[str, list[str]]:
    """Return {sector: [symbols]} mapping."""
    result: dict[str, list[str]] = {}
    for symbol, _, sector in NIFTY500:
        result.setdefault(sector, []).append(symbol)
    return result


def get_symbol_sector_map() -> dict[str, str]:
    """Return {trading_symbol: sector} for all N500 stocks."""
    return {symbol: sector for symbol, _, sector in NIFTY500}


if __name__ == "__main__":
    # Run after setting up Kite API key to fetch and print all tokens
    import os
    from kiteconnect import KiteConnect

    api_key     = os.environ.get("KITE_API_KEY", "")
    access_token = os.environ.get("KITE_ACCESS_TOKEN", "")

    if not api_key or not access_token:
        print("Set KITE_API_KEY and KITE_ACCESS_TOKEN env vars first.")
        raise SystemExit(1)

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    instruments = kite.instruments("NSE")
    token_map   = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}

    found = not_found = 0
    print(f"\n{'Symbol':<15} {'Token'}")
    print("─" * 30)
    for symbol, name, _ in NIFTY500:
        token = token_map.get(symbol)
        if token:
            print(f"{symbol:<15} {token}")
            found += 1
        else:
            print(f"{symbol:<15} NOT FOUND")
            not_found += 1

    print(f"\nFound: {found}  Not found: {not_found}  Total: {len(NIFTY500)}")
