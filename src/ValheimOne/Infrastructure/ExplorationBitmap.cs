using System;
using System.Collections;

namespace ValheimOne.Infrastructure;

// A view of the game's exploration storage, not a copy: received pixels must be
// written into the same bitmap that Minimap saves in the player's map data.
internal readonly struct ExplorationBitmap
{
    private readonly bool[]? _array;
    private readonly BitArray? _bits;

    private ExplorationBitmap(bool[]? array, BitArray? bits)
    {
        _array = array;
        _bits = bits;
    }

    public int Length => _bits?.Length ?? _array?.Length ?? 0;

    public bool this[int index]
    {
        get => _bits != null ? _bits[index] : _array![index];
        set
        {
            if (_bits != null)
            {
                _bits[index] = value;
            }
            else
            {
                _array![index] = value;
            }
        }
    }

    public static bool TryWrap(object? storage, out ExplorationBitmap bitmap)
    {
        bitmap = storage is BitArray bits
            ? new ExplorationBitmap(null, bits)
            : new ExplorationBitmap(storage as bool[], null);
        return bitmap._bits != null || bitmap._array != null;
    }

    public void CopyTo(bool[] destination)
    {
        if (destination.Length != Length)
        {
            throw new ArgumentException("Exploration bitmap dimensions do not match.", nameof(destination));
        }

        if (_bits != null)
        {
            _bits.CopyTo(destination, 0);
        }
        else
        {
            Array.Copy(_array!, destination, Length);
        }
    }

    public static implicit operator ExplorationBitmap(bool[] array) => new ExplorationBitmap(array, null);
}
