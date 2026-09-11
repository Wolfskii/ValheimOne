using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.CompilerServices;
using HarmonyLib;
using UnityEngine;

namespace ValheimOne.Modules;

// Handoff follows Container.RequestOpen: only the current owner grants a transfer.
// The reply additionally identifies the inventory revision. Receiving an RPC before
// its ZDO update must never allow crafting from an older inventory replica.
internal static class ChestOwnership
{
    private const string RequestRpc = "VO_CraftChestRequest";
    private const string ReplyRpc = "VO_CraftChestReply";
    private static readonly ConditionalWeakTable<Container, State> States = new();
    private static readonly Dictionary<string, Func<bool>> EnabledFeatures = new();
    private static int _nextRequest;

    internal enum Result { Ready, Pending, Unavailable }

    public static void Install(Harmony harmony, string feature, Func<bool> enabled)
    {
        EnabledFeatures[feature] = enabled;
        var awake = AccessTools.Method(typeof(Container), "Awake", Type.EmptyTypes);
        var register = AccessTools.Method(typeof(ChestOwnership), nameof(Register));
        if (Harmony.GetPatchInfo(awake)?.Postfixes.Any(p => p.owner == harmony.Id && p.PatchMethod == register) == true)
            return;
        harmony.Patch(
            awake,
            postfix: new HarmonyMethod(typeof(ChestOwnership), nameof(Register)));
    }

    private static void Register(Container __instance)
    {
        ZNetView? view = ChestScanner.GetNetworkView(__instance);
        if (view == null || !view.IsValid()) return;
        State state = States.GetValue(__instance, _ => new State());
        if (state.Registered) return;
        state.Registered = true;
        view.Register<ZPackage>(RequestRpc, (sender, package) => HandleRequest(__instance, sender, package));
        view.Register<ZPackage>(ReplyRpc, (sender, package) => HandleReply(__instance, sender, package));
    }

    public static Result Prepare(Container container, Player player)
    {
        Register(container);
        ZNetView? view = ChestScanner.GetNetworkView(container);
        if (view == null || !view.IsValid() || ChestScanner.IsBusy(container, view) ||
            !ChestScanner.HasContainerAccess(container, player.GetPlayerID())) return Result.Unavailable;

        State state = States.GetValue(container, _ => new State());
        ZDO zdo = view.GetZDO();
        float now = Time.realtimeSinceStartup;
        if (state.Request != 0)
        {
            if (state.Denied || now >= state.Deadline)
            {
                state.Request = 0;
                return Result.Unavailable;
            }

            // Both ownership and data must arrive; the RPC alone grants no write access.
            if (!state.Granted || !view.IsOwner() ||
                zdo.OwnerRevision != state.OwnerRevision || zdo.DataRevision < state.DataRevision)
                return Result.Pending;

            state.Request = 0;
            ChestScanner.RefreshInventory(container);
            return Result.Ready;
        }

        if (view.IsOwner())
        {
            ChestScanner.RefreshInventory(container);
            return Result.Ready;
        }

        // The game assigns unowned containers itself. Do not race that assignment.
        long owner = zdo.GetOwner();
        if (owner == 0) return Result.Pending;
        state.Request = _nextRequest = _nextRequest == int.MaxValue ? 1 : _nextRequest + 1;
        state.Owner = owner;
        state.Deadline = now + 5f;
        state.Granted = false;
        state.Denied = false;
        var request = new ZPackage();
        request.Write(state.Request);
        request.Write(player.GetPlayerID());
        view.InvokeRPC(owner, RequestRpc, request);
        return Result.Pending;
    }

    private static void HandleRequest(Container container, long sender, ZPackage package)
    {
        int request;
        long playerId;
        try { request = package.ReadInt(); playerId = package.ReadLong(); }
        catch (Exception) { return; }
        if (request <= 0 || sender == 0) return;
        ZNetView? view = ChestScanner.GetNetworkView(container);
        if (view == null || !view.IsValid() || !view.IsOwner()) return;
        var reply = new ZPackage();
        reply.Write(request);
        bool granted = EnabledFeatures.Values.Any(enabled => enabled()) && !ChestScanner.IsBusy(container, view) &&
            ChestScanner.HasContainerAccess(container, playerId);
        reply.Write(granted);
        if (granted)
        {
            ZDO zdo = view.GetZDO();
            zdo.SetOwner(sender);
            reply.Write((int)zdo.OwnerRevision);
            reply.Write((long)zdo.DataRevision);
            ZDOMan.instance.ForceSendZDO(sender, zdo.m_uid);
        }
        view.InvokeRPC(sender, ReplyRpc, reply);
    }

    private static void HandleReply(Container container, long sender, ZPackage package)
    {
        if (!States.TryGetValue(container, out State state) || state.Request == 0 || sender != state.Owner)
            return;
        try
        {
            if (package.ReadInt() != state.Request) return;
            bool granted = package.ReadBool();
            if (!granted) { state.Denied = true; return; }
            int ownerRevision = package.ReadInt();
            long dataRevision = package.ReadLong();
            if (ownerRevision < 0 || ownerRevision > ushort.MaxValue ||
                dataRevision < 0 || dataRevision > uint.MaxValue) return;
            state.OwnerRevision = (ushort)ownerRevision;
            state.DataRevision = (uint)dataRevision;
            state.Granted = true;
        }
        catch (Exception) { }
    }

    private sealed class State
    {
        public bool Registered;
        public int Request;
        public long Owner;
        public float Deadline;
        public bool Granted;
        public bool Denied;
        public ushort OwnerRevision;
        public uint DataRevision;
    }
}
