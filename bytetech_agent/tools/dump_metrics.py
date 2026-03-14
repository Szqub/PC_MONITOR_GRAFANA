"""
ByteTech Agent - Metrics Schema Dump Tool.
Runs the agent providers for a single cycle, collects metrics,
and safely prints their exact schema (measurement, tags, fields) to the terminal.
"""
import sys
import logging
from pprint import pprint

from bytetech_agent.config import load_config
from bytetech_agent.models.metrics import ProviderContext
from bytetech_agent.normalizers.influx_formatter import InfluxFormatter

# Providers
from bytetech_agent.providers.lhm_provider import LhmProvider
from bytetech_agent.providers.presentmon_provider import PresentMonProvider
from bytetech_agent.providers.display_provider import DisplayProvider
from bytetech_agent.providers.nvapi_provider import NvapiProvider
from bytetech_agent.providers.system_provider import SystemProvider

logging.basicConfig(level=logging.WARNING)

def dump_schema():
    print("="*60)
    print("ByteTech Agent - Metrics Schema Dump Tool")
    print("="*60)
    
    try:
        config = load_config()
    except Exception as e:
        print(f"Error loading config: {e}")
        sys.exit(1)

    context = ProviderContext(
        host_alias=config.metadata.host_alias,
        host_name="test-hostname",
        site=config.metadata.site,
        owner=config.metadata.owner,
    )

    providers = []
    
    # Init providers dynamically
    if config.providers.lhm_enabled:
        lhm_json_url = getattr(config.lhm, 'json_url', "http://127.0.0.1:8085")
        providers.append(LhmProvider(json_url=lhm_json_url))
        
    if config.providers.nvapi_provider_enabled:
        providers.append(NvapiProvider())
        
    if config.providers.presentmon_enabled:
        providers.append(PresentMonProvider(config.presentmon))
        
    if config.providers.display_provider_enabled:
        providers.append(DisplayProvider())
        
    if config.providers.system_provider_enabled:
        providers.append(SystemProvider())

    all_metrics = []

    print("\n[OK] Initializing Providers...")
    for provider in providers:
        success = provider.initialize()
        print(f"    - {provider.name}: {'SUCCESS' if success else 'FAILED/UNAVAILABLE'}")
        
        # Collect a sample
        try:
            metrics = provider.get_metrics(context)
            all_metrics.extend(metrics)
        except Exception as e:
            print(f"      Error collecting from {provider.name}: {e}")

    # Process normalized hardware metrics
    raw_hw = [m for m in all_metrics if m.measurement_name == "pc_hw_raw"]
    other_metrics = [m for m in all_metrics if m.measurement_name != "pc_hw_raw"]
    
    curated_hw = InfluxFormatter.normalize_to_curated(raw_hw)
    final_metrics = curated_hw + other_metrics

    # Group logically by Measurement Name
    schema_map = {}
    for m in final_metrics:
        m_name = m.measurement_name
        if m_name not in schema_map:
            schema_map[m_name] = {"samples": 0, "tags": set(), "fields": set()}
            
        schema_map[m_name]["samples"] += 1
        for k in m.tags.keys():
            schema_map[m_name]["tags"].add(k)
        for k in m.fields.keys():
            schema_map[m_name]["fields"].add(k)

    print("\n" + "="*60)
    print("LIVE RUNTIME SCHEMA")
    print("="*60)

    for m_name, meta in sorted(schema_map.items()):
        print(f"\nMeasurement: {m_name}")
        print(f"  Samples generated : {meta['samples']}")
        print(f"  Tags discovered   : {', '.join(sorted(list(meta['tags'])))}")
        
        print(f"  Fields discovered :")
        for field in sorted(list(meta["fields"])):
            print(f"      - {field}")

    print("\n[!] Shutting down providers safely...")
    for provider in providers:
        try:
            provider.shutdown()
        except:
            pass

if __name__ == "__main__":
    dump_schema()
