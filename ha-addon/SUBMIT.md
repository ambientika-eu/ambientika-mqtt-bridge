# Submitting to Home Assistant Add-on Store / HACS

There are two ways to get the Ambientika MQTT Bridge into Home Assistant:

1. **HACS (Home Assistant Community Store)** – fastest, available within days
2. **Official HA Add-on Store** – requires more review but reaches millions of users

---

## Option 1: HACS (Recommended – Start Here)

HACS is the most popular way to distribute custom HA integrations.
It has 500,000+ active users.

### Step 1: Create a dedicated add-on repository

The HA add-on must live in its own repository (not a subfolder of another repo).

```
Create new GitHub repository: ambientika-ha-addon
Structure:
  ambientika-ha-addon/
  ├── ambientika-mqtt-bridge/    (folder = add-on name)
  │   ├── config.yaml
  │   ├── Dockerfile
  │   ├── run.sh
  │   ├── README.md
  │   └── icon.png               (256x256 PNG)
  └── repository.yaml
```

### Step 2: Create repository.yaml

```yaml
name: Ambientika Add-ons
url: https://github.com/ambientika-eu/ambientika-ha-addon
maintainer: Ambientika / SUEDWIND <info@ambientika.eu>
```

### Step 3: Submit to HACS

1. Go to [github.com/hacs/default](https://github.com/hacs/default)
2. Fork the repository
3. Edit `appdaemon` or `integration` or `plugin` file – for add-ons: edit `appdaemon`
4. Add your repository URL:
   ```
   https://github.com/ambientika-eu/ambientika-ha-addon
   ```
5. Open a Pull Request
6. HACS bot runs automated checks
7. Once merged, users can add the store in HA → HACS → Settings → Custom Repositories

**Checklist for HACS submission:**
- [ ] Repository is public
- [ ] Has `hacs.json` in root
- [ ] Has a valid `README.md`
- [ ] Has `icon.png` (256x256)
- [ ] Has at least one release/tag

---

## Option 2: Official Home Assistant Add-on Store

This gets the add-on into the official "Add-on Store" tab in HA.
Requires more effort but gives maximum visibility.

### Requirements

- Standalone GitHub repository (one repo = one add-on)
- Must pass HA security audit
- Uses official `hassio-addons` base images
- Follows HA add-on schema strictly
- Uses GitHub Actions for CI/CD

### Steps

1. **Create standalone repo**: `ambientika-ha-addon`
2. **Add GitHub Actions workflow** for building and testing
3. **Open issue** at [github.com/home-assistant/addons-example](https://github.com/home-assistant/addons-example)
4. **Request review** from HA add-on maintainers
5. **Pass security audit** (automated + manual)

### Minimal GitHub Actions workflow

Create `.github/workflows/ci.yml`:
```yaml
name: CI
on: [push, pull_request]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build add-on
        uses: home-assistant/builder@master
        with:
          args: |
            --all
            --target /data/addon
            --docker-hub ghcr.io
```

---

## Quick Win: Manual Installation Guide for Users

While waiting for HACS/store approval, users can install via:

```yaml
# In Home Assistant configuration.yaml:
# Settings → Add-ons → Add-on Store → ⋮ → Repositories
# Add: https://github.com/ambientika-eu/ambientika-mqtt-bridge
```

This gives users immediate access before the official submission is approved.

---

## Timeline Estimate

| Path | Time to Available |
|------|------------------|
| Manual repository URL | Immediate (after repo created) |
| HACS submission | 1–4 weeks (PR review) |
| Official HA Add-on Store | 2–6 months (security audit) |

**Recommendation:** Start with HACS, announce via Home Assistant Community Forum
at [community.home-assistant.io](https://community.home-assistant.io),
then apply for official store listing.
