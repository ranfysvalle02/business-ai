/* ==========================================================================
   AI Stores — look/personality presets + font catalog
   --------------------------------------------------------------------------
   Single source of truth for the admin Appearance panel. Each preset is a
   bundle of `stores` document fields; applying one just copies these values
   onto the store (persisted via PATCH /api/stores/{id}). The storefront reads
   them back as CSS custom properties in _base.html, so a preset re-skins the
   entire site with no redeploy.

   `fonts_google` is the querystring for the Google Fonts css2 endpoint WITHOUT
   the leading `family=` (see _base.html):
       https://fonts.googleapis.com/css2?family={{ fonts_google }}&display=swap
   ========================================================================== */
(function () {
  "use strict";

  // ── Color helpers ───────────────────────────────────────────────────────
  // Storefront colors are stored as hex but injected as space-separated RGB
  // channels so Tailwind opacity modifiers work: rgb(var(--x) / <alpha>).

  // "#6366f1" -> "99 102 241". Falls back to black on malformed input.
  function hexToChannels(hex) {
    var m = /^#?([0-9a-f]{6})$/i.exec(String(hex || "").trim());
    if (!m) return "0 0 0";
    var n = parseInt(m[1], 16);
    return ((n >> 16) & 255) + " " + ((n >> 8) & 255) + " " + (n & 255);
  }

  function _rgb(hex) {
    return hexToChannels(hex).split(" ").map(Number);
  }

  // WCAG relative luminance of a hex color (0..1).
  function relLuminance(hex) {
    var c = _rgb(hex).map(function (v) {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }

  // WCAG contrast ratio between two hex colors (1..21).
  function contrastRatio(a, b) {
    var la = relLuminance(a);
    var lb = relLuminance(b);
    var hi = Math.max(la, lb);
    var lo = Math.min(la, lb);
    return (hi + 0.05) / (lo + 0.05);
  }

  // Pick the legible text color (near-black or white) to sit on a given bg.
  function bestTextOn(bg) {
    return contrastRatio(bg, "#ffffff") >= contrastRatio(bg, "#0a0a0a") ? "#ffffff" : "#0a0a0a";
  }

  // Tolerant hex parser: accepts "fff", "#FFF", "abc123", "#ABC123" and returns
  // a canonical "#rrggbb" (lowercase), or null if it isn't a valid hex color.
  function normalizeHex(v) {
    var s = String(v == null ? "" : v).trim().replace(/^#/, "");
    if (/^[0-9a-fA-F]{3}$/.test(s)) {
      s = s.split("").map(function (ch) { return ch + ch; }).join("");
    }
    return /^[0-9a-fA-F]{6}$/.test(s) ? "#" + s.toLowerCase() : null;
  }

  // ── HSL conversions (for palette derivation + contrast auto-fix) ──────────
  function hexToHsl(hex) {
    var c = _rgb(hex).map(function (v) { return v / 255; });
    var r = c[0], g = c[1], b = c[2];
    var max = Math.max(r, g, b), min = Math.min(r, g, b);
    var d = max - min;
    var h = 0, s = 0, l = (max + min) / 2;
    if (d !== 0) {
      s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
      if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h *= 60;
    }
    return { h: h, s: s * 100, l: l * 100 };
  }

  function hslToHex(h, s, l) {
    h = ((h % 360) + 360) % 360;
    s = Math.max(0, Math.min(100, s)) / 100;
    l = Math.max(0, Math.min(100, l)) / 100;
    var c = (1 - Math.abs(2 * l - 1)) * s;
    var x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    var m = l - c / 2;
    var r = 0, g = 0, b = 0;
    if (h < 60) { r = c; g = x; }
    else if (h < 120) { r = x; g = c; }
    else if (h < 180) { g = c; b = x; }
    else if (h < 240) { g = x; b = c; }
    else if (h < 300) { r = x; b = c; }
    else { r = c; b = x; }
    var toHex = function (v) { return ("0" + Math.round((v + m) * 255).toString(16)).slice(-2); };
    return "#" + toHex(r) + toHex(g) + toHex(b);
  }

  // Nudge colorHex's lightness (hue/sat preserved) until it clears `target`
  // contrast against againstHex. Returns the best result if target is
  // unreachable within the lightness range.
  function adjustForContrast(colorHex, againstHex, target) {
    target = target || 4.5;
    if (contrastRatio(colorHex, againstHex) >= target) return colorHex;
    var base = hexToHsl(colorHex);
    var dir = relLuminance(colorHex) >= relLuminance(againstHex) ? 1 : -1;
    var best = colorHex, bestC = contrastRatio(colorHex, againstHex);
    // Sweep lightness (both directions) at progressively lower saturation.
    // Lightness alone fixes most colors; desaturating rescues vivid mid-tones
    // that can't reach the target while fully saturated. Hue is preserved.
    var sats = [base.s];
    for (var sv = base.s - 20; sv > 0; sv -= 20) sats.push(sv);
    sats.push(0);
    for (var si = 0; si < sats.length; si++) {
      var s = sats[si];
      var found = null;
      [dir, -dir].forEach(function (step) {
        if (found) return;
        var l = base.l;
        for (var i = 0; i < 100; i++) {
          l += step;
          if (l < 0 || l > 100) break;
          var cand = hslToHex(base.h, s, l);
          var c = contrastRatio(cand, againstHex);
          if (c > bestC) { bestC = c; best = cand; }
          if (c >= target) { found = cand; break; }
        }
      });
      if (found) return found;
    }
    return best;
  }

  // Derive a full, AA-safe 6-color palette from a single brand color.
  // mode: "dark" (default) or "light". Every returned color passes the same
  // contrast bars the admin panel enforces (body text/labels 4.5:1, muted 3:1).
  function paletteFrom(brandHex, mode) {
    mode = mode === "light" ? "light" : "dark";
    var base = hexToHsl(brandHex);
    var neutral = base.s < 8;
    // Neutral/grayscale brands have no meaningful hue, so tint the palette at
    // s=0 and borrow a pleasant default accent hue for the pop color.
    var hue = neutral ? 0 : base.h;
    var tintS = neutral ? 0 : 24;
    var accentHue = neutral ? 217 : base.h + 160;
    var accentS = neutral ? 70 : Math.max(45, Math.min(100, base.s));

    // Keep the brand hue but ensure its button label is legible (nudges L only).
    var primary = adjustForContrast(brandHex, bestTextOn(brandHex), 4.5);
    var secondary = hslToHex(
      accentHue,
      accentS,
      mode === "dark" ? Math.max(52, Math.min(70, base.l + 8)) : Math.max(35, Math.min(55, base.l))
    );
    // Accent doubles as a button background, so make its label legible too.
    secondary = adjustForContrast(secondary, bestTextOn(secondary), 4.5);

    var background, surface, text, muted;
    if (mode === "dark") {
      background = hslToHex(hue, tintS, 7);
      surface = hslToHex(hue, neutral ? 0 : 20, 12);
      text = hslToHex(hue, neutral ? 0 : 12, 97);
      muted = hslToHex(hue, neutral ? 0 : 12, 66);
    } else {
      background = hslToHex(hue, neutral ? 0 : 30, 97);
      surface = hslToHex(hue, neutral ? 0 : 20, 100);
      text = hslToHex(hue, neutral ? 0 : 20, 12);
      muted = hslToHex(hue, neutral ? 0 : 12, 40);
    }
    // Guarantee AA for body text and >=3:1 for muted on both surfaces.
    text = adjustForContrast(text, background, 4.5);
    text = adjustForContrast(text, surface, 4.5);
    muted = adjustForContrast(muted, background, 3.0);
    muted = adjustForContrast(muted, surface, 3.0);
    return {
      theme_primary: primary,
      theme_secondary: secondary,
      theme_background: background,
      theme_surface: surface,
      theme_text: text,
      theme_text_secondary: muted,
      theme_on_primary: bestTextOn(primary),
      theme_on_secondary: bestTextOn(secondary)
    };
  }

  // Curated fonts. `google` is the css2 param for that single family (no
  // leading "family="). `family` is the CSS font stack applied via a variable.
  var FONTS = {
    inter:         { label: "Inter",          family: "'Inter', system-ui, sans-serif",        google: "Inter:wght@400;500;600;700" },
    "space-grotesk": { label: "Space Grotesk", family: "'Space Grotesk', system-ui, sans-serif", google: "Space+Grotesk:wght@500;600;700" },
    poppins:       { label: "Poppins",        family: "'Poppins', system-ui, sans-serif",      google: "Poppins:wght@500;600;700" },
    montserrat:    { label: "Montserrat",     family: "'Montserrat', system-ui, sans-serif",   google: "Montserrat:wght@500;600;700" },
    "dm-sans":     { label: "DM Sans",        family: "'DM Sans', system-ui, sans-serif",      google: "DM+Sans:wght@400;500;700" },
    "playfair":    { label: "Playfair Display", family: "'Playfair Display', Georgia, serif",  google: "Playfair+Display:wght@600;700;800" },
    fraunces:      { label: "Fraunces",       family: "'Fraunces', Georgia, serif",            google: "Fraunces:wght@500;600;700" },
    lora:          { label: "Lora",           family: "'Lora', Georgia, serif",                google: "Lora:wght@500;600;700" }
  };

  // Helper: build the fonts_google param from two font keys.
  function fontsGoogle(headingKey, bodyKey) {
    var h = FONTS[headingKey] || FONTS.inter;
    var b = FONTS[bodyKey] || FONTS.inter;
    return headingKey === bodyKey ? h.google : h.google + "&family=" + b.google;
  }

  // Each preset lists font keys; families + fonts_google are resolved below so
  // there's no chance of the two drifting out of sync.
  var RAW_PRESETS = [
    {
      key: "midnight",
      label: "Midnight",
      description: "Refined dark. Indigo + cyan, geometric sans.",
      headingFont: "space-grotesk",
      bodyFont: "inter",
      theme_radius: "0.75rem",
      heading_transform: "none",
      heading_spacing: "-0.02em",
      theme_primary: "#5e61f1",
      theme_secondary: "#22d3ee",
      theme_background: "#0b1120",
      theme_surface: "#151d2e",
      theme_text: "#f8fafc",
      theme_text_secondary: "#94a3b8"
    },
    {
      key: "editorial",
      label: "Editorial",
      description: "Light, magazine feel. Serif headlines on warm paper.",
      headingFont: "playfair",
      bodyFont: "inter",
      theme_radius: "0.25rem",
      heading_transform: "none",
      heading_spacing: "-0.01em",
      theme_primary: "#c2410c",
      theme_secondary: "#0f766e",
      theme_background: "#faf7f2",
      theme_surface: "#ffffff",
      theme_text: "#1c1917",
      theme_text_secondary: "#57534e"
    },
    {
      key: "luxe",
      label: "Luxe",
      description: "High-end boutique. Near-black + gold, uppercase serif.",
      headingFont: "fraunces",
      bodyFont: "inter",
      theme_radius: "0rem",
      heading_transform: "uppercase",
      heading_spacing: "0.14em",
      theme_primary: "#c9a227",
      theme_secondary: "#a8a29e",
      theme_background: "#0a0a0a",
      theme_surface: "#161615",
      theme_text: "#f5f5f4",
      theme_text_secondary: "#a3a3a3"
    },
    {
      key: "playful",
      label: "Playful",
      description: "Vibrant + friendly. Violet + lime, rounded everything.",
      headingFont: "poppins",
      bodyFont: "inter",
      theme_radius: "1.25rem",
      heading_transform: "none",
      heading_spacing: "-0.01em",
      theme_primary: "#a855f7",
      theme_secondary: "#a3e635",
      theme_background: "#1a1038",
      theme_surface: "#271a54",
      theme_text: "#faf5ff",
      theme_text_secondary: "#d8b4fe"
    },
    {
      key: "minimal",
      label: "Minimal",
      description: "Monochrome. Black, white, and one crisp line.",
      headingFont: "space-grotesk",
      bodyFont: "inter",
      theme_radius: "0.375rem",
      heading_transform: "none",
      heading_spacing: "-0.01em",
      theme_primary: "#0a0a0a",
      theme_secondary: "#737373",
      theme_background: "#ffffff",
      theme_surface: "#f5f5f5",
      theme_text: "#0a0a0a",
      theme_text_secondary: "#525252"
    }
  ];

  var PRESETS = RAW_PRESETS.map(function (p) {
    return {
      key: p.key,
      label: p.label,
      description: p.description,
      // The exact bundle of fields written onto the store document.
      fields: {
        style_preset: p.key,
        theme_primary: p.theme_primary,
        theme_secondary: p.theme_secondary,
        theme_background: p.theme_background,
        theme_surface: p.theme_surface,
        theme_text: p.theme_text,
        theme_text_secondary: p.theme_text_secondary,
        theme_on_primary: bestTextOn(p.theme_primary),
        theme_on_secondary: bestTextOn(p.theme_secondary),
        font_heading: (FONTS[p.headingFont] || FONTS.inter).family,
        font_body: (FONTS[p.bodyFont] || FONTS.inter).family,
        fonts_google: fontsGoogle(p.headingFont, p.bodyFont),
        theme_radius: p.theme_radius,
        heading_transform: p.heading_transform,
        heading_spacing: p.heading_spacing
      }
    };
  });

  window.STORE_FONT_CATALOG = FONTS;
  window.STORE_THEME_PRESETS = PRESETS;
  window.storeFontsGoogle = fontsGoogle;
  window.storeHexToChannels = hexToChannels;
  window.storeRelLuminance = relLuminance;
  window.storeContrastRatio = contrastRatio;
  window.storeBestTextOn = bestTextOn;
  window.storeNormalizeHex = normalizeHex;
  window.storeHexToHsl = hexToHsl;
  window.storeHslToHex = hslToHex;
  window.storeAdjustForContrast = adjustForContrast;
  window.storePaletteFrom = paletteFrom;
})();
