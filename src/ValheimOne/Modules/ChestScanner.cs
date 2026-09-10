using System;
using System.Collections.Generic;
using HarmonyLib;
using UnityEngine;

namespace ValheimOne.Modules;

internal sealed class ChestScanner
{
    private static readonly Func<Container, long, bool> CheckContainerAccess =
        AccessTools.MethodDelegate<Func<Container, long, bool>>(
            AccessTools.Method(typeof(Container), "CheckAccess", new[] { typeof(long) })
            ?? throw new MissingMethodException(nameof(Container), "CheckAccess"));

    private readonly List<Container> _cachedContainers = new List<Container>();
    private readonly List<Inventory> _accessibleInventories = new List<Inventory>();
    private readonly HashSet<Inventory> _seenInventories = new HashSet<Inventory>();
    private readonly bool _includeRemote;

    public ChestScanner(bool includeRemote = false)
    {
        _includeRemote = includeRemote;
    }

    public Container? FindContainer(Inventory inventory)
    {
        foreach (Container container in _cachedContainers)
        {
            if (container != null && ReferenceEquals(container.GetInventory(), inventory))
            {
                return container;
            }
        }

        return null;
    }

    private Player? _cachedPlayer;
    private Vector3 _cachedCenter;
    private float _cachedRange;
    private bool _cachedIgnoreWardedChests;
    private float _refreshAt;

    public IReadOnlyList<Inventory> GetInventories(
        Player player,
        float range,
        bool ignoreWardedChests,
        float cacheSeconds)
    {
        return GetInventories(
            player,
            player.transform.position,
            range,
            ignoreWardedChests,
            cacheSeconds);
    }

    public IReadOnlyList<Inventory> GetInventories(
        Player player,
        Vector3 center,
        float range,
        bool ignoreWardedChests,
        float cacheSeconds)
    {
        float clampedRange = Math.Max(1f, Math.Min(50f, range));
        float clampedCacheSeconds = Math.Max(1f, cacheSeconds);
        float now = Time.realtimeSinceStartup;

        if (!ReferenceEquals(_cachedPlayer, player) ||
            _cachedCenter != center ||
            _cachedRange != clampedRange ||
            _cachedIgnoreWardedChests != ignoreWardedChests ||
            now >= _refreshAt)
        {
            Refresh(
                player,
                center,
                clampedRange,
                ignoreWardedChests,
                now + clampedCacheSeconds);
        }

        RebuildAccessibleInventories(player, ignoreWardedChests);
        return _accessibleInventories;
    }

    private void Refresh(
        Player player,
        Vector3 center,
        float range,
        bool ignoreWardedChests,
        float refreshAt)
    {
        _cachedContainers.Clear();

        float rangeSquared = range * range;
        foreach (Container container in UnityEngine.Object.FindObjectsByType<Container>(
                     FindObjectsSortMode.None))
        {
            if (container == null)
            {
                continue;
            }

            Vector3 offset = container.transform.position - center;
            if (offset.sqrMagnitude <= rangeSquared)
            {
                _cachedContainers.Add(container);
            }
        }

        _cachedPlayer = player;
        _cachedCenter = center;
        _cachedRange = range;
        _cachedIgnoreWardedChests = ignoreWardedChests;
        _refreshAt = refreshAt;
    }

    private void RebuildAccessibleInventories(Player player, bool ignoreWardedChests)
    {
        _accessibleInventories.Clear();
        _seenInventories.Clear();

        long playerId = player.GetPlayerID();
        foreach (Container container in _cachedContainers)
        {
            ZNetView? networkView = container == null
                ? null
                : GetNetworkView(container);
            if (container == null ||
                networkView == null ||
                !networkView.IsValid() ||
                (!_includeRemote && !container.IsOwner()) ||
                (_includeRemote && IsBusy(container, networkView)))
            {
                continue;
            }

            // Remote copies are for crafting previews only. Mutation requires an acknowledged
            // ownership handoff and a freshly loaded inventory. Automation keeps owned-only scans.
            if (container.m_checkGuardStone &&
                !ignoreWardedChests &&
                !PrivateArea.CheckAccess(
                    container.transform.position,
                    radius: 0f,
                    flash: false))
            {
                continue;
            }

            // Container.CheckAccess is private in the runtime assembly. Resolve its open-instance
            // delegate once so access checks retain vanilla privacy semantics without emitting a
            // direct private-member call that Unity 6 Mono would reject.
            if (!HasContainerAccess(container, playerId))
            {
                continue;
            }

            if (_includeRemote) RefreshInventory(container);
            Inventory inventory = container.GetInventory();
            if (inventory != null && _seenInventories.Add(inventory))
            {
                _accessibleInventories.Add(inventory);
            }
        }
    }

    private static readonly Func<Container, bool> LoadInventory =
        AccessTools.MethodDelegate<Func<Container, bool>>(
            AccessTools.Method(typeof(Container), "Load", Type.EmptyTypes));

    internal static void RefreshInventory(Container container) => LoadInventory(container);

    internal static bool HasContainerAccess(Container container, long playerId) =>
        CheckContainerAccess(container, playerId);

    internal static bool IsBusy(Container container, ZNetView networkView) =>
        container.IsInUse() || networkView.GetZDO().GetInt(ZDOVars.s_inUse) != 0 ||
        (container.m_wagon != null && container.m_wagon.InUse());

    internal static ZNetView? GetNetworkView(Container container)
    {
        return container.m_rootObjectOverride != null
            ? container.m_rootObjectOverride
            : container.GetComponent<ZNetView>();
    }
}
