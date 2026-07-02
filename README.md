# Climate Forecasts

Servicio de datos para las **475 referencias únicas de pronóstico** que cubren
las 10.601 localidades de ClimateProyectar.

- Consulta siempre las 475 referencias.
- Pausa de 2 segundos entre consultas.
- Fallback oficial moderno → histórico remoto → respaldo histórico local.
- Conserva el último dato válido ante fallos temporales.
- Publica un archivo JSON por referencia en `/docs/pronosticos`.

## GitHub Pages

Configurar Pages desde `main` y `/docs`.

## Cron externo

Horario recomendado:

```cron
35 */3 * * *
```

Cuerpo:

```json
{
  "ref": "main"
}
```

Endpoint:

```text
https://api.github.com/repos/mtgproyect/climate-forecasts/actions/workflows/actualizar-pronosticos.yml/dispatches
```
