# Notice

Copyright (C) 2026 Lombo-Forge contributors. Licensed under the GNU General Public License v3.0
(see [LICENSE](LICENSE)).

Not affiliated with or endorsed by the Jellyfin project. "Jellyfin" is used only to say what this
works with.

## Credits

- **IP geolocation by DB-IP.com** (https://db-ip.com). The "IP to Country Lite" database is licensed under
  [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/). LomboVisa downloads it at run time
  (`lombo-visa geoip update`); the file is never committed to this repository and never redistributed with it. If you
  publish a page or a report that shows data derived from it, keep this credit with it.
- The MMDB file format is documented by MaxMind (https://maxmind.github.io/MaxMind-DB/). `src/lombo_visa/mmdb.py` is an
  independent reader written from that description; no MaxMind code is used.
- Cinzel (type for banners): SIL Open Font License 1.1.
