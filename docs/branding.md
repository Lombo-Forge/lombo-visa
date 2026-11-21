# The Lombo look

## Contents

- [The rule](#the-rule)
- [This app's mark](#this-apps-mark)
- [Colours and type](#colours-and-type)
- [The seal and the L](#the-seal-and-the-l)
- [What every app needs](#what-every-app-needs)
- [What to avoid](#what-to-avoid)

## The rule

**The Lombo L is in every icon.** Each app has its own seal, and a small mark of its own sits in
the open corner of the L. Each app also gets one wordmark trick (Tier's letters step down,
Shrink's get smaller from S to K, Shelf's F leans).

## This app's mark

LomboVisa's mark is a **passport stamp with a small globe**: a double ring around a globe (a circle, one meridian and the equator), tilted 12
degrees like a stamp pressed by hand, in the ember orange with the inner ring in the muted text colour. It sits right of the L's stem and above its
foot, clear of both. The wordmark trick: "VISA" is tilted 3 degrees, as if stamped. The source is `assets/icon.svg` and `assets/banner.svg`;
the PNGs are rendered from them with the Cinzel font installed (see the `forge-branding` skill in the workspace).

## Colours and type

| Use | Value |
|---|---|
| Background | `#0d0a08` |
| Surface | `#1a140f` |
| Borders | `#2c231a`, `#443528` |
| Text, muted | `#f4ede3`, `#b3a696` |
| Ember gradient | `#ffb347` to `#ff6a1a` to `#e23d0f` |

Headings and wordmarks: Cinzel (SIL Open Font License). Body text: Geist.

## The seal and the L

`assets/icon.svg` is the seal: a ring with the gradient, an inner frame line, the L, and the stamp. Banners use the same seal on the left and
the wordmark on the right.

## What every app needs

- `assets/icon.svg`, plus a 512 px PNG for the container registry and GitHub.
- `assets/banner.svg`, shown at the top of the README (1200 x 300).
- A 1280 x 640 social preview image, uploaded in the repository settings.

SVG text uses fonts the viewer may not have, so export the PNG versions with the font installed
and use those where exact lettering matters.

## What to avoid

- No Tolkien names, scripts, rings or symbols. The feel comes from engraved capitals, a map-style
  frame and the forge colours, not from borrowed imagery.
- No logos, names or colours of other products. "For Jellyfin" is fine in a description; never
  imply the Jellyfin project made the app.
