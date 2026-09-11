using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using BepInEx;
using HarmonyLib;
using UnityEngine;
using ValheimOne.LiveMap;

[BepInPlugin("com.humangenome.seedpreview.probe", "Seed Preview Exporter", "0.1.0")]
public sealed class SeedPreviewExporter : BaseUnityPlugin
{
    private static string Seed = "";
    private string _output = "";
    private readonly Stopwatch _clock = Stopwatch.StartNew();
    private WorldGenerator? _generator;
    private BinaryWriter? _height;
    private BinaryWriter? _biomes;
    private byte[]? _terrainPixels;
    private byte[]? _biomePixels;
    private readonly List<string> _samples = new();
    private int _row;
    private int _size;
    private bool _finished;
    private double _samplingStarted;
    private bool _terrainComplete;
    private string _mapMetadata = "";
    private const float Extent = 10500f;
    private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

    private void Awake()
    {
        Seed = Environment.GetEnvironmentVariable("VO_PREVIEW_SEED") ?? "";
        _output = Environment.GetEnvironmentVariable("VO_PREVIEW_OUTPUT") ?? "";
        _size = int.TryParse(Environment.GetEnvironmentVariable("VO_PREVIEW_SIZE"), out int size) ? size : 1024;
        if (!Regex.IsMatch(Seed, "^[A-Za-z0-9]{1,10}$") || !Path.IsPathRooted(_output) || _size < 64 || _size > 2048)
            throw new InvalidOperationException("Invalid isolated exporter inputs");
        Directory.CreateDirectory(_output);
        var harmony = new Harmony("com.humangenome.seedpreview.probe");
        harmony.Patch(
            AccessTools.Method(typeof(World), "GetCreateWorld"),
            new HarmonyMethod(typeof(SeedPreviewExporter), nameof(CreatePreviewWorld)));
        WriteProgress("starting", 0);
        Logger.LogInfo("Seed preview exporter initialized");
    }

    private static bool CreatePreviewWorld(string name, ref World __result)
    {
        if (name != "SeedPreview") throw new InvalidOperationException("Exporter requires its own SeedPreview world");
        __result = new World(name, Seed);
        return false;
    }

    private void Update()
    {
        if (_finished) return;
        try
        {
            if (_clock.Elapsed.TotalSeconds > 300) throw new TimeoutException("World export timed out");
            if (_terrainComplete)
            {
                if (ZoneSystem.instance == null || !ZoneSystem.instance.LocationsGenerated) return;
                ExportLocations();
                string completed = Regex.Replace(_mapMetadata, "\"seconds\":[0-9.]+", "\"seconds\":" + _clock.Elapsed.TotalSeconds.ToString("F2", Inv));
                File.WriteAllText(Path.Combine(_output, "map.json"), completed);
                WriteProgress("complete", 100);
                _finished = true;
                Logger.LogInfo("SEED_EXPORT_COMPLETE " + Seed + " " + _size);
                Application.Quit(0);
                return;
            }
            if (_generator == null)
            {
                if (WorldGenerator.instance == null || ZoneSystem.instance == null) return;
                if (Environment.GetEnvironmentVariable("VO_PREVIEW_DEFER_TERRAIN") == "1" && !ZoneSystem.instance.LocationsGenerated) return;
                var world = AccessTools.Field(typeof(WorldGenerator), "m_world").GetValue(WorldGenerator.instance) as World;
                if (world == null || world.m_seedName != Seed) return;
                _samplingStarted = _clock.Elapsed.TotalSeconds;
                _generator = WorldGenerator.instance;
                _height = new BinaryWriter(File.Create(Path.Combine(_output, "height.f32")));
                _biomes = new BinaryWriter(File.Create(Path.Combine(_output, "biomes.u16")));
                _terrainPixels = new byte[_size * _size * 4];
                _biomePixels = new byte[_size * _size * 4];
                Logger.LogInfo("Sampling the initialized game world at " + _samplingStarted.ToString("F2", Inv) + " seconds");
            }
            int stop = Math.Min(_size, _row + 8);
            for (; _row < stop; _row++)
            {
                float z = Extent - ((_row + 0.5f) * Extent * 2f / _size);
                for (int xIndex = 0; xIndex < _size; xIndex++)
                {
                    float x = -Extent + ((xIndex + 0.5f) * Extent * 2f / _size);
                    var biome = _generator.GetBiome(x, z);
                    float height = _generator.GetHeight(x, z, out Color mask);
                    if (float.IsNaN(height) || float.IsInfinity(height)) throw new InvalidDataException("Non-finite terrain sample");
                    _height!.Write(height);
                    _biomes!.Write((ushort)biome);
                    int offset = (_row * _size + xIndex) * 4;
                    MapShading.Compose(biome, height, mask.a, x, z, Extent * 2f / _size).WriteRgba(_terrainPixels!, offset);
                    MapColor plain = height < 30f || x * x + z * z > 10470f * 10470f
                        ? new MapColor(0.088f, 0.140f, 0.240f) : BiomePalette.Get(biome, 30f);
                    plain.WriteRgba(_biomePixels!, offset);
                    if (_row % 128 == 64 && xIndex % 128 == 64)
                        _samples.Add("{\"x\":" + x.ToString("R", Inv) + ",\"z\":" + z.ToString("R", Inv) + ",\"height\":" + height.ToString("R", Inv) + ",\"biome\":" + (int)biome + "}");
                }
            }
            if (_row % 64 == 0) WriteProgress("sampling", _row * 100 / _size);
            if (_row < _size) return;
            _height!.Dispose(); _biomes!.Dispose();
            File.WriteAllBytes(Path.Combine(_output, "terrain.rgba"), _terrainPixels!);
            File.WriteAllBytes(Path.Combine(_output, "biomes.rgba"), _biomePixels!);
            var palette = new List<string>();
            foreach (Heightmap.Biome biome in Enum.GetValues(typeof(Heightmap.Biome)))
                palette.Add("\"" + ((int)biome).ToString(Inv) + "\":\"" + biome + "\"");
            string metadata = "{\"seed\":\"" + Seed + "\",\"gameVersion\":\"" + Version.GetVersionString() + "\",\"size\":" + _size + ",\"extent\":10500,\"waterLevel\":30,\"seconds\":" + _clock.Elapsed.TotalSeconds.ToString("F2", Inv) + ",\"biomes\":{" + string.Join(",", palette) + "},\"samples\":[" + string.Join(",", _samples) + "]}";
            File.WriteAllText(Path.Combine(_output, "phase-metrics.json"),
                "{\"terrainStartSeconds\":" + _samplingStarted.ToString("F2", Inv) +
                ",\"samplingSeconds\":" + (_clock.Elapsed.TotalSeconds - _samplingStarted).ToString("F2", Inv) + "}");
            _mapMetadata = metadata;
            File.WriteAllText(Path.Combine(_output, "preview.json"), metadata);
            _terrainComplete = true;
            WriteProgress("locations", 0);
            Logger.LogInfo("SEED_TERRAIN_READY " + Seed + " " + _clock.Elapsed.TotalSeconds.ToString("F2", Inv));
        }
        catch (Exception error)
        {
            _finished = true;
            _height?.Dispose(); _biomes?.Dispose();
            File.WriteAllText(Path.Combine(_output, "error.txt"), error.GetType().Name + ": " + error.Message);
            Logger.LogError(error);
            Application.Quit(1);
        }
    }

    private void WriteProgress(string state, int percent)
    {
        string path = Path.Combine(_output, "progress.json");
        File.WriteAllText(path + ".tmp", "{\"state\":\"" + state + "\",\"percent\":" + percent + "}");
        if (File.Exists(path)) File.Delete(path);
        File.Move(path + ".tmp", path);
    }

    private void ExportLocations()
    {
        var locations = AccessTools.Field(typeof(ZoneSystem), "m_locationInstances").GetValue(ZoneSystem.instance) as IDictionary;
        if (locations == null) throw new InvalidDataException("World location catalogue unavailable");
        var rows = new List<string>();
        foreach (DictionaryEntry pair in locations)
        {
            object item = pair.Value;
            object location = AccessTools.Field(item.GetType(), "m_location").GetValue(item);
            string name = (string)AccessTools.Field(location.GetType(), "m_prefabName").GetValue(location);
            var position = (Vector3)AccessTools.Field(item.GetType(), "m_position").GetValue(item);
            bool placed = (bool)AccessTools.Field(item.GetType(), "m_placed").GetValue(item);
            if (!Regex.IsMatch(name, "^[A-Za-z0-9_ -]+$")) throw new InvalidDataException("Unexpected location name");
            rows.Add("{\"name\":\"" + name + "\",\"x\":" + position.x.ToString("R", Inv) + ",\"z\":" + position.z.ToString("R", Inv) + ",\"placed\":" + (placed ? "true" : "false") + "}");
        }
        File.WriteAllText(Path.Combine(_output, "locations.json"), "[" + string.Join(",", rows) + "]");
    }
}

namespace ValheimOne.LiveMap
{
    // The shared shading code uses this same radius in the released renderer.
    internal static class WorldMapRenderer
    {
        public const int WorldRadius = 10500;
    }
}
