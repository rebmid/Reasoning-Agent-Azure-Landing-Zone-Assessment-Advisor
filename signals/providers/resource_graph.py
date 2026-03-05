"""Resource Graph signal providers — VNets, firewalls, public IPs, route tables."""
from __future__ import annotations

import time
from typing import Any

from signals.types import SignalResult, SignalStatus


def _query_rg(query: str, subscriptions: list[str], *, top: int = 1000) -> SignalResult:
    """Execute a Resource Graph query and return a SignalResult.

    Paginates automatically if the result set exceeds *top* rows.
    """
    from azure.mgmt.resourcegraph import ResourceGraphClient
    from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
    from collectors.azure_client import get_shared_credential

    start = time.perf_counter_ns()
    try:
        credential = get_shared_credential()
        client = ResourceGraphClient(credential)
        all_items: list = []
        skip_token: str | None = None

        while True:
            options = QueryRequestOptions(
                result_format="objectArray",
                top=top,
            )
            if skip_token:
                options.skip_token = skip_token

            request = QueryRequest(
                subscriptions=subscriptions,
                query=query,
                options=options,
            )
            response = client.resources(request)
            page = response.data if isinstance(response.data, list) else []  # type: ignore[assignment]
            all_items.extend(page)

            skip_token = getattr(response, "skip_token", None)
            if not skip_token or not page:
                break

        ms = (time.perf_counter_ns() - start) // 1_000_000
        return SignalResult(
            signal_name="",  # caller sets this
            status=SignalStatus.OK,
            items=all_items,
            raw={"query": query, "count": len(all_items), "pages": 1 + (len(all_items) // top if all_items else 0)},
            duration_ms=ms,
        )
    except Exception as e:
        ms = (time.perf_counter_ns() - start) // 1_000_000
        return SignalResult(
            signal_name="",
            status=SignalStatus.ERROR,
            error_msg=str(e),
            duration_ms=ms,
        )


def fetch_azure_firewalls(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where type =~ 'microsoft.network/azurefirewalls'
    | project name, resourceGroup, location, id,
              sku=properties.sku.name,
              policyId=properties.firewallPolicy.id
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:azure_firewall"
    return r


def fetch_vnets(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where type =~ 'microsoft.network/virtualnetworks'
    | project name, resourceGroup, location, id,
              addressSpace=properties.addressSpace.addressPrefixes,
              subnets=array_length(properties.subnets),
              peerings=array_length(properties.virtualNetworkPeerings),
              ddosProtectionPlan=properties.enableDdosProtection
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:vnets"
    return r


def fetch_public_ips(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where type =~ 'microsoft.network/publicipaddresses'
    | project name, resourceGroup, location, id,
              sku=sku.name,
              allocationMethod=properties.publicIPAllocationMethod,
              associatedTo=properties.ipConfiguration.id
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:public_ips"
    return r


def fetch_route_tables(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where type =~ 'microsoft.network/routetables'
    | project name, resourceGroup, location, id,
              routes=array_length(properties.routes),
              subnets=array_length(properties.subnets)
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:route_tables"
    return r


def fetch_nsg_list(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where type =~ 'microsoft.network/networksecuritygroups'
    | project name, resourceGroup, location, id,
              rules=array_length(properties.securityRules)
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:nsgs"
    return r


# ── VNet Peering Details ─────────────────────────────────────────
def fetch_vnet_peerings(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where type =~ 'microsoft.network/virtualnetworks'
    | mvexpand peering = properties.virtualNetworkPeerings
    | project vnetName=name, vnetId=id, resourceGroup, location,
              peeringName=peering.name,
              peeringState=peering.properties.peeringState,
              remoteVnetId=peering.properties.remoteVirtualNetwork.id,
              allowForwardedTraffic=peering.properties.allowForwardedTraffic,
              allowGatewayTransit=peering.properties.allowGatewayTransit,
              useRemoteGateways=peering.properties.useRemoteGateways
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:vnet_peerings"
    # Enrich raw with summary stats
    connected = sum(1 for p in r.items if (p.get("peeringState") or "").lower() == "connected")
    r.raw = r.raw or {}
    r.raw["total_peerings"] = len(r.items)
    r.raw["connected"] = connected
    r.raw["disconnected"] = len(r.items) - connected
    return r


# ── ExpressRoute / VPN Gateways ──────────────────────────────────
def fetch_gateway_inventory(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where type in~ (
        'microsoft.network/virtualnetworkgateways',
        'microsoft.network/expressroutecircuits',
        'microsoft.network/expressrouteports'
      )
    | project name, type, resourceGroup, location, id,
              gatewayType=properties.gatewayType,
              vpnType=properties.vpnType,
              sku=sku.name,
              activeActive=properties.activeActive,
              enableBgp=properties.enableBgp,
              circuitStatus=properties.serviceProviderProvisioningState
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:gateway_inventory"
    vpn_gw = sum(1 for i in r.items if (i.get("gatewayType") or "").lower() == "vpn")
    er_gw = sum(1 for i in r.items if (i.get("gatewayType") or "").lower() == "expressroute")
    er_circuits = sum(1 for i in r.items if "expressroutecircuits" in (i.get("type") or "").lower())
    r.raw = r.raw or {}
    r.raw["vpn_gateways"] = vpn_gw
    r.raw["expressroute_gateways"] = er_gw
    r.raw["expressroute_circuits"] = er_circuits
    return r


# ── Azure Bastion ────────────────────────────────────────────────
def fetch_bastion_hosts(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where type =~ 'microsoft.network/bastionhosts'
    | project name, resourceGroup, location, id,
              sku=sku.name
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:bastion_hosts"
    return r


# ── WAF / Front Door / Application Gateway ───────────────────────
def fetch_waf_frontdoor(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where type in~ (
        'microsoft.network/frontdoors',
        'microsoft.network/applicationgateways',
        'microsoft.cdn/profiles',
        'microsoft.network/frontdoorwebapplicationfirewallpolicies',
        'microsoft.network/applicationgatewaywebapplicationfirewallpolicies'
      )
    | project name, type, resourceGroup, location, id,
              sku=sku.name,
              wafPolicy=coalesce(
                properties.webApplicationFirewallPolicyLink.id,
                properties.firewallPolicy.id
              )
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:waf_frontdoor"
    app_gw = sum(1 for i in r.items if "applicationgateways" in (i.get("type") or "").lower())
    front_door = sum(1 for i in r.items if "frontdoors" in (i.get("type") or "").lower())
    waf_policies = sum(1 for i in r.items if "firewallpolicies" in (i.get("type") or "").lower())
    r.raw = r.raw or {}
    r.raw["application_gateways"] = app_gw
    r.raw["front_doors"] = front_door
    r.raw["waf_policies"] = waf_policies
    return r


# ── Private DNS Zones ────────────────────────────────────────────
def fetch_private_dns_zones(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where type =~ 'microsoft.network/privatednszones'
    | project name, resourceGroup, location, id,
              recordSets=properties.numberOfRecordSets,
              vnetLinks=properties.numberOfVirtualNetworkLinks,
              autoRegistration=properties.numberOfVirtualNetworkLinksWithRegistration
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:private_dns_zones"
    auto_reg = sum(1 for z in r.items if (z.get("autoRegistration") or 0) > 0)
    r.raw = r.raw or {}
    r.raw["total_zones"] = len(r.items)
    r.raw["zones_with_auto_registration"] = auto_reg
    return r


# ── Tag Compliance ───────────────────────────────────────────────
def fetch_tag_compliance(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where tags != '{}'
    | summarize tagged=count() by type
    | join kind=fullouter (
        Resources | summarize total=count() by type
    ) on type
    | project type=coalesce(type, type1),
              total=coalesce(total, 0),
              tagged=coalesce(tagged, 0)
    | extend untagged = total - tagged
    | order by untagged desc
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:tag_compliance"
    total = sum(i.get("total", 0) for i in r.items)
    tagged = sum(i.get("tagged", 0) for i in r.items)
    r.raw = r.raw or {}
    r.raw["total_resources"] = total
    r.raw["tagged_resources"] = tagged
    r.raw["tag_coverage_pct"] = round(tagged / max(total, 1) * 100, 1)
    return r


# ── Disk Encryption Status ───────────────────────────────────────
def fetch_disk_encryption(subscriptions: list[str]) -> SignalResult:
    query = """
    Resources
    | where type =~ 'microsoft.compute/disks'
    | project name, resourceGroup, id,
              encryptionType=properties.encryption.type,
              diskState=properties.diskState,
              osType=properties.osType
    | extend isEncrypted = (encryptionType != 'EncryptionAtRestWithPlatformKey'
                            and isnotempty(encryptionType))
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:disk_encryption"
    total = len(r.items)
    cmk = sum(1 for d in r.items if d.get("isEncrypted", False))
    r.raw = r.raw or {}
    r.raw["total_disks"] = total
    r.raw["customer_managed_key"] = cmk
    r.raw["platform_key_only"] = total - cmk
    return r


# ── Custom RBAC Role Definitions ─────────────────────────────────
def fetch_custom_roles(subscriptions: list[str]) -> SignalResult:
    query = """
    AuthorizationResources
    | where type =~ 'microsoft.authorization/roledefinitions'
    | where properties.type == 'CustomRole'
    | project name=properties.roleName, id,
              description=properties.description,
              actions=properties.permissions[0].actions,
              assignableScopes=properties.assignableScopes
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:custom_roles"
    wildcard = sum(
        1 for role in r.items
        if any("*" in str(a) for a in (role.get("actions") or []))
    )
    r.raw = r.raw or {}
    r.raw["total_custom_roles"] = len(r.items)
    r.raw["wildcard_action_roles"] = wildcard
    return r


# ── Policy Exemptions ────────────────────────────────────────────
def fetch_policy_exemptions(subscriptions: list[str]) -> SignalResult:
    query = """
    PolicyResources
    | where type =~ 'microsoft.authorization/policyexemptions'
    | project name, id,
              category=properties.exemptionCategory,
              expiresOn=properties.expiresOn,
              policyAssignmentId=properties.policyAssignmentId
    """
    r = _query_rg(query, subscriptions)
    r.signal_name = "resource_graph:policy_exemptions"
    waiver = sum(1 for e in r.items if (e.get("category") or "").lower() == "waiver")
    mitigated = sum(1 for e in r.items if (e.get("category") or "").lower() == "mitigated")
    r.raw = r.raw or {}
    r.raw["total_exemptions"] = len(r.items)
    r.raw["waivers"] = waiver
    r.raw["mitigated"] = mitigated
    return r
