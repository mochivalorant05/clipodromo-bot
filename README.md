# El Clipódromo · bot de clips

Cada 5 minutos (GitHub Actions) el bot:

1. Revisa los clips de la última hora de los streamers de `bot/config.yaml`.
2. Descarta todo lo relacionado con **apuestas y casinos** y detecta los virales:
   varios clips del mismo momento del directo, o visitas por minuto muy por encima de lo normal para ese streamer.
3. Los descarga y los edita en vertical (1080x1920): fondo desenfocado, título, subtítulos automáticos y crédito al streamer.
4. Los publica en TikTok, Instagram y YouTube a través de Buffer.
   Los primeros clips se publican solo tras aprobarlos en un issue de este repositorio (responde **ok** o **no**).

Secretos necesarios (Settings → Secrets and variables → Actions):
`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `BUFFER_API_KEY`.
