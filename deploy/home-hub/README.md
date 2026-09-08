# Hub casero: Mac Mini + Cloudflare Tunnel

La Mac Mini corre el server (Python nativo, sin Docker) y `cloudflared` abre un
túnel saliente: **cero puertos abiertos en tu router** y HTTPS gratis en tu
dominio. Con Cloudflare Access encima, auth por email para hasta 50 usuarios.

```
Internet → Cloudflare → (túnel cifrado saliente) → Mac Mini :8000 (solo localhost)
```

## 1. Registrar el dominio (~10 USD/año)

La vía más corta: **Cloudflare Registrar** (a precio de costo, y el dominio ya
queda en Cloudflare, que es lo que necesitamos para el túnel).

1. Creá cuenta en https://dash.cloudflare.com
2. **Domain Registration → Register Domains** → buscá el nombre (p. ej.
   `paperchameleon.app`, `mipapers.com`…) y compralo con el método de pago
3. Alternativa: comprarlo en Namecheap/Porkbun (suele ser más barato el primer
   año) y en Cloudflare **Add a site** → cambiar los nameservers a los que te da
   Cloudflare

## 2. Preparar la Mac Mini

```bash
# Homebrew (si no está): https://brew.sh
sudo pmset -a sleep 0 disksleep 0          # que no se duerma nunca
sudo pmset -a autorestart 1                # se prende sola si se corta la luz
```

- Cloná el repo: `git clone https://github.com/Faridmurzone/pdf-traslator`
- Python 3.10+ (en la Mini vieja: `brew install python@3.13` o el instalador de
  python.org)

## 3. Crear el túnel

```bash
brew install cloudflared
cloudflared tunnel login                   # abre el browser, elegí tu dominio
cloudflared tunnel create paper-chameleon  # anotá el ID (o mirá ~/.cloudflared/*.json)
```

## 4. Instalar todo

```bash
cd deploy/home-hub
TUNNEL_ID=<el-id> ./setup.sh /ruta/al/repo papers.tudominio.com
```

El script: instala dependencias en `.venv`, genera `~/.cloudflared/config.yml`,
instala **dos LaunchAgents** (`ar.paperchameleon.app` y
`ar.paperchameleon.tunnel`) que arrancan solos y se reinician si caen, y rutea
el DNS. Verificá:

```bash
curl -s http://127.0.0.1:8000/api/meta
curl -s https://papers.tudominio.com/api/meta
```

## 5. (Recomendado) Cloudflare Access = auth del hub

Zero Trust dashboard → **Access → Applications → Add** → self-hosted → dominio
`papers.tudominio.com` → política: *Emails* con los correos de tu grupo
(OTP por email, gratis hasta 50 usuarios). Nadie más toca el hub.

## 6. Keys

- **Cada usuario la suya** (default): se ingresan en la UI, viajan solo con cada
  pedido y quedan en el `localStorage` de cada navegador
- **Key del hub compartida**: editá
  `~/Library/LaunchAgents/ar.paperchameleon.app.plist` y completá
  `ANTHROPIC_API_KEY`, luego
  `launchctl kickstart -k gui/$(id -u)/ar.paperchameleon.app`

## Operación

| Qué | Cómo |
|---|---|
| Logs | `~/.pdf-chameleon/*.log` |
| Reiniciar app | `launchctl kickstart -k gui/$(id -u)/ar.paperchameleon.app` |
| Reiniciar túnel | `launchctl kickstart -k gui/$(id -u)/ar.paperchameleon.tunnel` |
| Actualizar versión | `git pull && .venv/bin/pip install -e ".[web,providers]" && launchctl kickstart -k gui/$(id -u)/ar.paperchameleon.app` |
| Datos (historial + comunidad) | `~/.pdf-translator/` en la Mini — backupeá esa carpeta |

Límites a saber: Cloudflare free pisa uploads en 100MB (papers <20MB: sin
problema) y no hay timeouts que afecten (los jobs corren en background).
