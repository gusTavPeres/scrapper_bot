"""Canário contra a OLX REAL: pega mudança de layout/bloqueio que teste
sintético não vê (o flight data morreu em runtime e o bot passou meses
raspando ~5 de 50 anúncios por página sem ninguém notar).
Uso: ./venv/bin/python test_live_olx.py   (~1 min, ~30 requisições)"""
import asyncio
import re
import tempfile
from pathlib import Path

import avaliador
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

        # "&_rsc" já na URL: a OLX tem de responder direto, sem o 307 (1 requisição por página)
        u = s._montar_url({"termo": "geladeira"}, 1) + "&_rsc"
        r = await sessao.get(u, timeout=30, headers={"RSC": "1"}, allow_redirects=False)
        assert r.status_code == 200 and "x-component" in r.headers.get("content-type", ""), \
            f"&_rsc não evita mais o redirecionamento: HTTP {r.status_code}"
        print("OK &_rsc: resposta direta, sem redirecionamento")

        # imóveis: cidade e bairro no caminho (a OLX tem de ECOAR o local, senão ignorou),
        # quartos/vagas como lista, e a ficha com condomínio, área e quartos
        b = {"tipo": "imovel", "estado": "sp", "termo": "", "preco_max": 4000,
             "filtros_extra": {"cidade": "São Paulo", "imovel": "apartamento", "quartos_min": 2, "vagas_min": 1}}
        ads = await s._pagina_de_busca(sessao, s._montar_url(b, 1), exige="loc_")
        assert ads and len(ads) >= 40, f"aluguel: {ads and len(ads)} anúncios"
        fichas = [avaliador.dados_imovel(x) for x in ads]
        assert all(x["categoria"] == "Apartamentos (aluguel)" for x in ads), {x["categoria"] for x in ads}
        assert all(f["cidade"] == "sao paulo" and f["quartos"] >= 2 and f["vagas"] >= 1 for f in fichas), fichas[:3]
        assert sum(1 for f in fichas if f["cond"] and f["area"]) > 0.6 * len(ads), "condomínio/área sumiram da ficha"
        assert all(0 < x["preco"] <= 4600 for x in ads) and len(s.filtrar_produtos(b, ads)) == len(ads)
        print(f"OK aluguel: {len(ads)} apartamentos na cidade, 2+ quartos, 1+ vaga, ficha completa")
        b["filtros_extra"]["bairros"] = ["Moema"]
        ads = await s._pagina_de_busca(sessao, s._montar_url(b, 1), exige="loc_")
        assert ads and all(avaliador.dados_imovel(x)["bairro"] == "moema" for x in ads), "bairro na URL não filtrou"
        print(f"OK aluguel por bairro: {len(ads)} anúncios, todos no bairro")
        try:
            await s._pagina_de_busca(sessao, s._montar_url({**b, "filtros_extra": {"cidade": "Cidade Que Nao Existe"}}, 1),
                                     exige="loc_")
            raise AssertionError("cidade inexistente passou sem erro")
        except AssertionError:
            raise
        except Exception as e:      # a OLX ora ignora o local (LocalDesconhecido), ora devolve 5xx
            print(f"OK cidade inexistente: {type(e).__name__} em vez do estado inteiro")
        desc = s.extrair_descricao((await s._baixar(sessao, ads[0]["url"])).text)
        assert len(desc) > 10, "descrição do imóvel vazia"

    # mediana do mercado: a busca SEM o teto, como o bot mede 1x/dia
    bid = database.criar_busca({"nome": "canário mercado", "termo": "rtx 3090", "preco_max": 6000})
    await s._atualizar_mercado(database.obter_busca(bid))
    m = database.obter_busca(bid)["mercado"]
    assert m.get("mediana") and m["n"] >= 10 and m["mediana"] > 3000, f"mercado da 3090: {m}"
    print(f"OK mercado sem teto: mediana R$ {m['mediana']} de {m['n']} anúncios")

    busca = {"id": 0, "nome": "canário", "termo": "rtx 4090"}
    r = await s.raspar_busca(busca, "monitor")
    assert r and len({x["list_id"] for x in r}) == len(r)
    print(f"OK raspar_busca (monitor): {len(r)} anúncios únicos")
    print("=> OLX OK")


asyncio.run(main())
