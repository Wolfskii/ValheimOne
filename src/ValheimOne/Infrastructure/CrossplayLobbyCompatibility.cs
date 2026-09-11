using System;
using System.Collections;
using System.Reflection;
using HarmonyLib;

namespace ValheimOne.Infrastructure;

// A closed crossplay lobby can outlive its owner. A collision with that lobby
// must take the game's ordinary regeneration path instead of dereferencing Owner.
internal static class CrossplayLobbyCompatibility
{
    private static MemberInfo? _lobbies;
    private static MemberInfo? _owner;
    private static MethodInfo? _sessionUpdated;
    private static object? _regenerateState;

    public static void Apply(Harmony harmony)
    {
        MethodInfo check = AccessTools.Method(typeof(ZPlayFabMatchmaking), "OnCheckJoinCodeSuccess")
            ?? throw new MissingMethodException(nameof(ZPlayFabMatchmaking), "OnCheckJoinCodeSuccess");
        ParameterInfo[] parameters = check.GetParameters();
        if (parameters.Length != 1) throw new InvalidOperationException("Unknown crossplay join-code response shape");
        _lobbies = FindMember(parameters[0].ParameterType, "Lobbies");
        Type collectionType = MemberType(_lobbies);
        if (!typeof(IList).IsAssignableFrom(collectionType) || !collectionType.IsGenericType)
            throw new InvalidOperationException("Unknown crossplay lobby collection shape");
        _owner = FindMember(collectionType.GetGenericArguments()[0], "Owner");
        _sessionUpdated = AccessTools.Method(typeof(ZPlayFabMatchmaking), "OnSessionUpdated")
            ?? throw new MissingMethodException(nameof(ZPlayFabMatchmaking), "OnSessionUpdated");
        ParameterInfo[] updateParameters = _sessionUpdated.GetParameters();
        if (updateParameters.Length != 1 || !updateParameters[0].ParameterType.IsEnum)
            throw new InvalidOperationException("Unknown crossplay session state shape");
        _regenerateState = Enum.Parse(updateParameters[0].ParameterType, "RegenerateJoinCode");
        harmony.Patch(check, prefix: new HarmonyMethod(typeof(CrossplayLobbyCompatibility), nameof(CheckJoinCodePrefix)));
    }

    private static bool CheckJoinCodePrefix(ZPlayFabMatchmaking __instance, object __0)
    {
        if (__0 == null || !(Read(_lobbies!, __0) is IList lobbies) || lobbies.Count != 1)
            return true;
        object? lobby = lobbies[0];
        if (lobby != null && Read(_owner!, lobby) != null) return true;

        // The code is already present in the lobby index, even if its owner left.
        // Preserve the result and run the same transition used for any other collision.
        _sessionUpdated!.Invoke(__instance, new[] { _regenerateState });
        return false;
    }

    private static MemberInfo FindMember(Type type, string name) =>
        (MemberInfo?)AccessTools.Field(type, name) ?? AccessTools.Property(type, name)
        ?? throw new MissingMemberException(type.FullName, name);

    private static Type MemberType(MemberInfo member) =>
        member is FieldInfo field ? field.FieldType : ((PropertyInfo)member).PropertyType;

    private static object? Read(MemberInfo member, object instance) =>
        member is FieldInfo field ? field.GetValue(instance) : ((PropertyInfo)member).GetValue(instance);
}
