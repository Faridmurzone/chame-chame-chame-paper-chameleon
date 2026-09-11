# Hub en AWS EC2 t3.micro (free tier)

## 0. Cuenta y expectativa de costo

- Cuentas **viejas** (pre-jul 2025): 12 meses de free tier — 750 h/mes de
  t2/t3.micro (cubre 24/7), 30GB EBS gp3 y 100GB/mes de egress: **$0**
- Cuentas **nuevas**: modelo de créditos (~$100 al registrarte + hasta $100 por
  actividades, free plans ~6 meses). t3.micro queda cubierto ese período; después
  ~**$7.5/mes** on-demand (o Lightsail 1GB ~$5/mes todo incluido)
- **Poné un Budgets alarm en $5** (Billing → Budgets → Create) para que jamás
  haya sorpresas
- El costo grande del proyecto es la API del LLM (~$0.5-1 por paper con
  Sonnet-class), no el hosting. Con "cada usuario su key" (default), el hosting
  es tu único costo

## 1. Crear la instancia (10 min)

1. Consola AWS → **EC2 → Launch instance**
2. Nombre: `paper-chameleon` · AMI: **Ubuntu Server 24.04 LTS** · Tipo:
   **t3.micro** (elegible free tier)
3. Key pair: creá una y **descargá el `.pem`** (no se puede recuperar)
4. Storage: **30GB gp3** (cubre el free tier)
5. Security group `paper-chameleon-sg`:
   - **SSH (22) → My IP**
   - Nada más si vas a usar el túnel (recomendado). Para una prueba rápida
     sumá Custom TCP **8000 → 0.0.0.0/0** y después lo sacás
6. Launch → esperá "Running" y anotá la **IP pública**

## 2. Instalar el hub

```bash
chmod 400 key.pem
ssh -i key.pem ubuntu@<IP>
```

Dentro de la instancia:

```bash
git clone https://github.com/Faridmurzone/pdf-traslator
sudo REPO_DIR=/opt/pdf-chameleon ./pdf-traslator/deploy/aws-ec2/setup-ec2.sh
```

El script activa 2GB de swap (la instancia tiene 1GB de RAM), instala Docker,
levanta el compose y te deja la app en `http://<IP>:8000` para probar.

## 3. Público: túnel de Cloudflare (recomendado)

Cero puertos web abiertos, HTTPS en tu dominio y auth con Access:

1. Necesitás el **dominio en Cloudflare** (ver `deploy/home-hub/README.md` §1)
2. Zero Trust dashboard → **Networks → Tunnels → Create tunnel** (Cloudflare
   Connector) → nombre `paper-chameleon` → copiá el **TOKEN** del one-liner
3. En la instancia:
   ```bash
   sudo TUNNEL_TOKEN=<token> ./pdf-traslator/deploy/aws-ec2/setup-ec2.sh
   ```
4. En el dashboard del túnel: **Public Hostname** → `papers.tudominio.com` →
   `HTTP://localhost:8000`
5. Security group final: **solo SSH (22) desde My IP**
6. (Opcional) **Access** sobre el hostname para auth por email (hasta 50
   usuarios gratis)

Nota: el proxy de Cloudflare free pisa uploads en 100MB — papers <20MB: sin
problema.

## Operación

| Qué | Cómo |
|---|---|
| Logs | `docker compose logs -f` en `/opt/pdf-chameleon` |
| Reiniciar app | `cd /opt/pdf-chameleon && sudo docker compose restart` |
| Actualizar versión | `cd /opt/pdf-chameleon && sudo git pull && sudo docker compose up -d --build` |
| Keys del hub | editá `/opt/pdf-chameleon/.env` y `sudo docker compose up -d` |
| Backup | `tar czf hub-data.tgz /opt/pdf-chameleon/data` (cron semanal + copialo a S3/otro lado) |
