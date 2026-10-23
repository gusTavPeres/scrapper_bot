"""Canário contra a OLX REAL: pega mudança de layout/bloqueio que teste
sintético não vê (o flight data morreu em runtime e o bot passou meses
raspando ~5 de 50 anúncios por página sem ninguém notar).
Uso: ./venv/bin/python test_live_olx.py   (~20 s, ~8 requisições)"""
import asyncio
import re
import tempfile
from pathlib import Path

import database
import scheduler as s

database.DB_PATH = Path(tempfile.mkdtemp()) / "canario.db"     # nunca o banco real
database.init_db()


async def main():
    async with s._sessao() as sessao:
        async def pagina(busca, n=1):
            html = (await s._baixar(sessao, s._montar_url(busca, n))).text
            ads = s.extrair_anuncios(html)
            sort = re.search(r'\\?"sort\\?":\\?"(\w+)', html)
            return ads, sort and sort.group(1)

        # caminho principal: payload RSC (3x menor) tem de trazer EXATAMENTE o mesmo
        for b in ({"termo": "geladeira"}, {"termo": "rtx 3090", "preco_max": 6000}):
            u = s._montar_url(b, 1)
            rsc = await s._baixar(sessao, u, headers={"RSC": "1"})
            assert "x-component" in rsc.headers.get("content-type", ""), "OLX parou de servir RSC"
            via_rsc = s._anuncios_do_flight(rsc.text)
            via_html = s.extrair_anuncios((await s._baixar(sessao, u)).text)
            assert via_rsc == via_html and via_rsc is not None, "RSC != HTML"
            print(f"OK RSC == HTML ({b['termo']}): {len(via_rsc)} anúncios, "
                  f"{len(rsc.content)//1024} KB vs HTML maior")

        ads, sort = await pagina({"termo": "geladeira"})
        assert ads is not None, "flight data AUSENTE — layout da OLX mudou"
        assert len(ads) >= 40, f"só {len(ads)} anúncios na pág 1 (esperado ~50)"
        assert sort == "date", f"ordenação '{sort}' (sf=1 deveria ser 'date')"
        datas = [a["data_postagem"] for a in ads[:10]]
        assert datas == sorted(datas, reverse=True), f"não está por data: {datas}"
        a = ads[0]
        assert a["list_id"] and a["preco"] > 0 and a["imagem"] and a["data_postagem"][:2] == "20", a
        print(f"OK busca: {len(ads)} anúncios por data, ex.: {a['titulo'][:40]} R$ {a['preco']:.0f}")

        ads2, _ = await pagina({"termo": "geladeira"}, 2)
        # por data, anúncio novo entre as 2 requisições empurra 1-2 da pág 1 p/ a 2
        # (o raspador descarta repetidos); paginação quebrada repete ~todos
        repetidos = {x["list_id"] for x in ads2} & {x["list_id"] for x in ads}
        assert ads2 and len(repetidos) < 10, f"pág 2 repete {len(repetidos)} da pág 1"
        print(f"OK paginação: pág 2 com {len(ads2)} anúncios ({len(repetidos)} empurrados da pág 1)")

        ads, _ = await pagina({"termo": "geladeira", "estado": "go"})
        assert ads and len(ads) >= 20, f"busca por estado devolveu {ads and len(ads)}"
        print(f"OK estado-go: {len(ads)} anúncios")

        ads, _ = await pagina({"termo": "linea", "tipo": "carro", "estado": "go",
                               "filtros_extra": {"ano_min": 2010, "ano_max": 2014}})
        assert ads, "categoria de carros vazia"
        anos = [int(m.group(1)) for x in ads if (m := re.search(r"Ano: (\d{4})", x["atributos"]))]
        assert anos and all(2010 <= y <= 2014 for y in anos), f"filtro de ano falhou: {anos}"
        print(f"OK carros: {len(ads)} anúncios, anos {min(anos)}-{max(anos)}")

        desc = s.extrair_descricao((await s._baixar(sessao, ads[0]["url"])).text)
        assert len(desc) > 10, "descrição vazia — JSON-LD do anúncio mudou?"
        print(f"OK descrição: {desc[:60]!r}")

    busca = {"id": 0, "nome": "canário", "termo": "rtx 4090"}
    r = await s.raspar_busca(busca, "monitor")
    assert r and len({x["list_id"] for x in r}) == len(r)
    print(f"OK raspar_busca (monitor): {len(r)} anúncios únicos")
    print("=> OLX OK")


asyncio.run(main())
