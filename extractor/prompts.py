"""
prompts.py
==========
Contains all LLM prompts and classification definitions for certificate screening,
field extraction, and ISO scope classification.
"""

GENERAL_TYPE_DEFS = """
GENERAL_TYPE must be EXACTLY one of these six values:
 
1. Silicon Wafer
   A specialized plant that takes ultra-pure polysilicon, grows it into crystalline
   ingots, and slices/polishes those ingots into thin wafers of precise diameters
   for use as substrate in semiconductor manufacturing.
 
2. Fabrication
   A front-end fab where unpatterned silicon wafers undergo sequential processes
   (photolithography, etching, diffusion, deposition, etc.) to build semiconductor
   devices at the wafer level. Key indicators: wafer diameter (100 mm, 200 mm,
   300 mm), technology node (180 nm, 7 nm, etc.). The output is processed wafers,
   NOT packaged chips or modules.
 
3. IC Assembly
   A back-end packaging facility where individual silicon dies are attached to lead
   frames or substrates, wire-bonded, and encapsulated into discrete or integrated
   circuit packages. Output examples: MOSFETs, Rectifiers, DRAM, audio ICs, power
   ICs, gate drivers, amplifier ICs, RF chips — any named/numbered chip or IC
   product sold in a package (DIP, QFN, TO-220, BGA, etc.).
 
4. Assembly
   A plant where electronic components are mounted and soldered onto printed circuit
   boards (PCBs) or assembled into finished devices whose function depends on
   embedded electronics (PCBA-based products). Output examples: drivers, LCD modules
   with driver ICs, power supplies, DC/DC converters, inverters, UPS, touch panels,
   industrial controllers, medical devices with electronics, complete instruments,
   IoT devices, communication modules.
 
   HVAC and climate control products (air conditioners, heat pumps, chillers,
   VRF systems, fan coil units, air handling units) are Assembly — they contain
   inverter drive boards, electronic control boards, and embedded thermostats
   whose removal would stop the unit functioning.
 
5. Factory
   A general manufacturing site that processes raw or intermediate materials into
   mechanical, electromechanical, or passive components — without producing PCBA-
   based products or packaged ICs. Output examples: metal parts, housings, frames,
   fasteners, lead frames, stamped parts, passive components (resistors, capacitors,
   inductors), bare PCBs, cables, wire harnesses, connectors, switches, pipes, resin
   parts, refrigeration mechanical parts, raw panels (glass/LCD panel only, no
   driver IC), and contract assembly of purely mechanical sub-assemblies.
 
   Factory is the default category for physical manufacturing only when the output
   is primarily mechanical, passive, structural, material, or non-electronic products.
 
   If the finished product performs sensing, measurement, control, automation,
   communication, computation, or intelligent functions, prefer Assembly.
 
6. Undefined
   Reserved for companies whose primary business produces no manufactured physical
   goods. Examples:
   - Pure software, SaaS, cloud platforms, AI models
   - Consulting, engineering design services, simulation software, analytics
   - Financial products and payment services (fuel cards, charge cards, prepaid
     cards, fleet payment systems)
   - Pure telematics, tracking, or fleet management services with no manufactured
     hardware product (vehicle tracking subscriptions, GPS services, route planning)
   - Insurance, logistics brokerage, or other purely service-based businesses
 
   Note: if a company sells both a physical tracking device AND a tracking service,
   classify by the physical product. Only use Undefined when the offering is
   entirely service-based with no manufactured hardware.
"""


# ── Dual-Flag Certificate Screening Prompt ──────────────────────────────────────
# Distinguishes between:
# 1) Is it a certificate at all? (is_cert: true/false)
# 2) Is it specifically an ISO or IATF management system certificate? (is_iso_iatf: true/false)
CERT_SCREENING_PROMPT = """You are an expert document analyst specializing in official certificates and management system standards.

The image below is a document page. You must answer two distinct questions:
1) Is this document a certificate of any kind? (is_cert)
2) Is this document specifically an ISO or IATF management system certificate? (is_iso_iatf)

Answer ONLY with valid JSON — no markdown fences, no extra text:
{
  "is_cert": true or false,
  "is_iso_iatf": true or false,
  "cert_type": "<exact name or standard of certificate as written, or null if not a certificate>",
  "reason": "<short 1-sentence reason>"
}

DEFINITIONS & RULES:

PART 1 — CERTIFICATE CHECK (`is_cert`):
- `is_cert: true` if this document is a certificate, formal accreditation, declaration of conformity, quality award, license, or formal compliance document.
- `is_cert: false` if this is NOT a certificate (e.g. product catalog, datasheet, marketing brochure, website screenshot, invoice, bill of lading, product manual, general corporate policy, photo of facility/products, technical drawing).

PART 2 — ISO / IATF MANAGEMENT SYSTEM CHECK (`is_iso_iatf`):
- `is_iso_iatf: true` ONLY IF the document is a genuine ISO management system certificate (e.g. ISO 9001, ISO 14001, ISO 45001, ISO 13485, ISO 27001, ISO 50001, ISO 22000, etc.) or an IATF certificate (e.g. IATF 16949) issued by an independent accredited certification body (e.g. TÜV, BSI, SGS, DNV, Bureau Veritas, DEKRA, Intertek, etc.) TO an organization.
  * The standard must appear visibly as the standard being certified against.
  * If the document is self-authored or signed by the company itself rather than an external accredited certification body, `is_iso_iatf: false`.
  * Ignore reference standards like ISO/IEC 17025, 17021, 17065 when they merely describe the testing lab or accreditation body.
  * Sector schemes that merely incorporate ISO but have their own scheme title (e.g. AS9100, IRIS, TISAX) -> `is_cert: true`, but `is_iso_iatf: false`.
- If `is_cert: false` -> `is_iso_iatf: false` always.
- If it is a non-ISO certificate (e.g. CE mark, UL listing, RoHS declaration, patent certificate, employee training certificate, government MSME/ZED badge, industry excellence award) -> `is_cert: true`, but `is_iso_iatf: false`.

The page may be rotated (90°, 180°, or 270°) — mentally correct orientation before deciding.
"""


# ── Page Field Extraction Prompt ──────────────────────────────────────────────
CERT_EXTRACTION_PROMPT = """You are an expert at reading ISO and IATF certification documents.

The input is an image of a certificate page. The document may be written in English, German, Chinese, or Korean.

IMPORTANT: The page may be rotated (90°, 180°, or 270°). Mentally rotate if needed.

CRITICAL ACCURACY RULE:
Copy ALL numbers, digits, and characters EXACTLY as they appear. Never change any digit.

Extract the following fields:

1) site_name — The name of the certified site or organization as written on the certificate.
   This is typically the company name or branch/facility name that received the certification.
   Do NOT include the address. Return null if not visible.

2) address — FULL postal address of the CERTIFIED SITE(S) only.
   Ignore "Head office"/"Registered office" if a separate certified site address exists.
   Multiple certified sites → join with " | ".
   Do NOT include the company name. Return null if not visible.

3) scope — Certification scope text EXACTLY as written. Return null if not visible.

4) iso_type — ISO/IATF standard exactly as written (e.g. ISO 9001:2015, IATF 16949:2016). Return null if not visible.

5) issued_date — The NEWEST date among ALL issue, granting, or effective dates on the page.

   STEP 1 — Collect every date associated with any of these labels:
     Issue date, Date of issue, Issued on, Current certificate, Current date of granting,
     Date of granting, Effective date, Valid from, Certification date, Re-certification date,
     Latest issue, Current issue, Date of first issue, Renewal date.

   STEP 2 — Also collect dates labeled:
     Initial certification date, Certified since, First issued, Original issue date.
     These are LOWER PRIORITY — only use them if NO other issue/granting date exists.

   STEP 3 — From all dates collected in STEP 1 (and STEP 2 if STEP 1 is empty),
     pick the NEWEST (most recent) date. That is the issued_date.

   STEP 4 — The expiry/valid-until date is NEVER the issued_date. Keep them separate.

   Return null if no issue or granting date is visible.

6) expiration_date — The FURTHEST FUTURE date on the certificate — this is when
   the certificate truly expires and must be renewed.

   STEP 1 — Collect every date associated with any of these labels (these all
   represent the final expiry boundary):
     Expiry date, Expiration date, Valid until, Valid to, Certificate valid until,
     Recertification date, Next recertification date, Recertification validity date,
     Re-certification date, Renewal date (when used as the final boundary).

   STEP 2 — Also collect dates labeled:
     Surveillance validity date, Surveillance audit date, Validity date.
     These are LOWER PRIORITY — only use them if NO recertification/expiry date exists.

   STEP 3 — From all dates collected in STEP 1 (and STEP 2 if STEP 1 is empty),
     pick the FURTHEST FUTURE (most distant) date. That is the expiration_date.

   STEP 4 — This date must always be later than issued_date. Never reuse issued_date here.

   Return null if no expiry or recertification date is visible.

7) issued_date_format — Format of issued_date (e.g. "YYYY-MM-DD", "DD/MM/YYYY"). Return null if issued_date is null.

8) expiration_date_format — Format of expiration_date. Return null if expiration_date is null.

SPECIAL PATTERN: "Valid from X to Y" → issued_date=X, expiration_date=Y.

Return ONLY valid JSON with exactly these keys — no markdown, no extra text:
{
  "site_name": "...",
  "address": "...",
  "scope": "...",
  "iso_type": "...",
  "issued_date": "...",
  "issued_date_format": "...",
  "expiration_date": "...",
  "expiration_date_format": "..."
}
"""


# ── Scope Classification Prompt ───────────────────────────────────────────────
CERT_SCOPE_CLASSIFY_PROMPT = """You are an expert manufacturing analyst. You will classify a single ISO/IATF
certificate based ONLY on its certified SCOPE text below — do not assume anything
about the company beyond what this scope states.

---CERTIFICATE SCOPE---
{scope}
---END SCOPE---

{general_type_defs}

CLASSIFICATION RULES — follow in order:

RULE 0 — PURE SOFTWARE / SERVICES → Undefined
If the scope describes only software, SaaS, cloud platforms, information systems,
digital platforms, AI models, consulting, engineering services, simulation software,
design services, financial products, or pure tracking/telematics services with no
manufactured physical product, return "Undefined".

Do not infer manufacturing from words such as:
"platform", "software", "application", "service", "analytics", "management",
"planning", "intelligence", "digital", "cloud", "simulation", "tracking", or "card".

EXCEPTION: Companies that physically build, integrate, or assemble computing
hardware systems (supercomputers, HPC clusters, quantum computers, server
systems, or similar physical computing infrastructure) are NOT Undefined —
classify them as Assembly. The presence of "system software", "support", or
"consulting" alongside physical hardware building does not override this.

RULE 0B — PURE SALES, TRADING, AND DISTRIBUTION → Undefined

The following are NEVER manufacturing activities and always → Undefined:
- Sales of components, parts, or products (without manufacturing them)
- Trading companies, wholesalers, or importers/exporters
- Distribution of electronic components, semiconductors, or any other goods
- Commercializzazione (Italian: commercialization/trading)
- Purchasing offices, procurement services, or buying agents

If the scope describes ONLY selling, trading, distributing, sourcing, or
commercializing products made by others — with no mention of manufacturing,
fabrication, assembly, production, or processing — return "Undefined" immediately.

Key signals for pure sales/distribution:
- "Sales of ...", "Sale and distribution of ...", "Supply of ..."
- "Commercializzazione di ..." (Italian)
- "Vertrieb von ..." (German: distribution of)
- "Négoce de ..." (French: trading of)

RULE 1 — OUTPUT-BASED CLASSIFICATION
Classify based on what the PRIMARY physical output/product described in the scope is.

RULE 2 — SEMICONDUCTOR CLASSIFICATION
The word "semiconductor" alone is not enough. Use these sub-rules in order:

  2a. WAFER-LEVEL OUTPUT = Fabrication
      Foundry services, wafer fabrication, technology nodes (e.g. 180 nm, 28 nm),
      or wafer diameters (200 mm, 300 mm) → Fabrication.

  2b. BROAD SEMICONDUCTOR CATEGORIES = Fabrication
      Scope describes broad semiconductor categories by signal type or technology
      domain (Analog, Digital, RF, Power, Logic semiconductors) without naming
      specific packaged chip products → Fabrication.

  2c. NAMED/PACKAGED IC PRODUCTS = IC Assembly
      Output is semiconductor chips: IC product families, chip part numbers,
      integrated circuits → IC Assembly.

      Do NOT classify as IC Assembly when semiconductors are merely components
      used inside larger systems, robots, machines, automation equipment,
      networking products, IoT products, or smart devices.

      PHOTONIC ICs: PICs, InP chips, laser chips, optical chips, and laser
      assemblies built on a monolithic semiconductor platform → IC Assembly, even
      if the word "assembly" appears in the product name.

RULE 3 — PCBA-BASED PRODUCTS = Assembly
Classify as Assembly when the scope's primary output is either:
1. A PCBA itself, or
2. A finished product whose primary function requires active electronic circuitry.

Explicit mention of PCBs, PCBAs, microcontrollers, processors, ICs, or embedded
electronics is sufficient but NOT required.

HVAC RULE: Air conditioners, heat pumps (air source, ground source), chillers,
VRF/VRV systems, fan coil units, and air handling units are Assembly. These
products contain inverter drive PCBAs and electronic control boards whose removal
would render the unit non-functional. Do NOT classify HVAC products as Factory.

Medical devices, instruments, automation equipment, robots, and measurement
equipment should be classified as Assembly only when there is evidence of embedded
electronics or PCBAs. If no such evidence exists, do not assume Assembly.

RULE 3A — FINISHED EQUIPMENT WITH EMBEDDED ELECTRONICS
The key question:
  Would the product still function if all PCBAs and electronic control circuitry
  were removed?
  If no → Assembly.
  If yes → Factory.

RULE 4 — MECHANICAL / PASSIVE / WIRE HARNESS = Factory (not Assembly)
Wire harnesses, cable assemblies, pipe processing, resin bending/bonding, stamped
metal parts, mechanical sub-assemblies, and contract parts assembly for other
manufacturers are Factory. Key test: does the scope's output contain active
electronics (ICs, microcontrollers, transistors on a PCB)? If no → Factory.

RULE 5 — TIEBREAKER
When the scope spans multiple categories, classify by the product line that
represents the majority of the scope text. If still unclear, prefer the
higher-complexity category using this order:

    Fabrication > IC Assembly > Assembly > Factory

RULE 6 — UNDEFINED (last resort)
Return "Undefined" ONLY when the scope is entirely non-physical: purely software,
SaaS, cloud services, platforms, consulting, engineering services, analytics,
design services, financial products, or pure tracking/telematics services — with
no manufactured physical product whatsoever.

IF THE SCOPE IS EMPTY, MISSING, OR TOO VAGUE TO DETERMINE A PHYSICAL OUTPUT, return
"Undefined" for site_iso_type and null for iso_product.

---

ALSO extract "iso_product": a short noun-phrase naming the core product/equipment
category being certified, taken directly from the scope's own wording, trimmed of
generic filler such as "the manufacture and supply of", "design and development of",
"the provision of", "for the X industry", "and related services", "Ltd", "Inc", etc.
Keep only the product/equipment noun phrase itself.

LANGUAGE RULE: iso_product must always be in English. If the scope is not in
English, translate the product noun phrase and return it as:
"English Translation (Original Text)"

Example (English scope):
scope: "THE MANUFACTURE AND SUPPLY OF MEASURING AND CONTROL EQUIPMENT FOR THE
ELECTRONICS INDUSTRY."
iso_product: "MEASURING AND CONTROL EQUIPMENT"

If no clear product can be identified from the scope, return null for iso_product.

YOUR RESPONSE MUST BE EXACTLY THIS JSON OBJECT AND NOTHING ELSE.
NO explanation. NO reasoning. NO markdown. NO preamble.
Just the raw JSON:
{{"site_iso_type": "<your answer here>", "iso_product": "<short product phrase or null>"}}

Valid values for site_iso_type: "Silicon Wafer", "Fabrication", "IC Assembly", "Assembly", "Factory", "Undefined"
"""

CERT_LANGUAGE_PROMPT = """Analyze the attached certificate document image and identify the primary language of the body text and content of the certificate.

IMPORTANT INSTRUCTIONS:
1. Do NOT decide based on the company name, logo, or postal address (which may be in English or local language).
2. Focus on the main legal text, certification statement, standard headings, terms, dates text, and body wording of the certificate.
3. Identify the predominant natural language of the certificate content (e.g., "English", "German", "French", "Chinese", "Spanish", "Japanese", "Italian", "Portuguese", etc.).
4. If multiple languages are used equally (e.g., bilingual certificate like Chinese/English or German/English), return "Bilingual (Language1/Language2)" or the primary language.

YOUR RESPONSE MUST BE STRICT VALID JSON AND NOTHING ELSE:
{
  "language": "<Name of language, e.g. English, German, French, etc.>",
  "confidence": 1.0
}
"""
