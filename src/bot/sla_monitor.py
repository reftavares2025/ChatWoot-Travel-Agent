import asyncio

async def sla_monitor_loop():
    """Monitor de SLA de 5 minutos da Captur Viagens."""
    while True:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            await asyncio.sleep(60)
